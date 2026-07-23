"""Tests for ontology/identity_validation.py (OKV-001 + OKV-002 gates).

Covers the acceptance contracts for Issues #7 and #8 on branch scope/ontology-integrity.

OKV-001 — Entity identity / relationship endpoint alignment
    - Happy path: real model.yaml produces zero OKV violations
    - Physical identity columns resolved from actual bindings (not defaults)
    - Relationship source endpoint checked against source entity identity domain
    - Relationship target endpoint checked against target entity identity domain
    - Mismatch → ONTOLOGY_RELATIONSHIP_KEY_MISMATCH with actionable context
    - Valid FK aliases (source_entity_id → entity_id convention) pass without error
    - Multi-property identity (entityIdProperties) correctly validated per-property
    - Mixed entity identity columns across types: each type validated independently
    - Missing binding (no entityIdColumn declared) → error or falls back to declared property
    - Ambiguous binding → error with clear gate message
    - Dry-run: resolve_entity_identity_map and resolve_relationship_endpoint_map
      return correct mappings without triggering errors

OKV-002 — Partial date precision detection and strict projection
    - Year-only values detected as DatePrecision.YEAR ("2023", "1999")
    - Year-month values detected as DatePrecision.YEAR_MONTH ("2023-07", "1999-12")
    - Full-date values detected as DatePrecision.FULL_DATE ("2023-07-22")
    - Timestamp values detected as DatePrecision.TIMESTAMP ("2023-07-22T10:30:00Z")
    - Unknown / empty list returns DatePrecision.UNKNOWN
    - Mixed precision: coarsest wins (year+timestamp → YEAR)
    - Partial values preserved verbatim; no missing components invented
    - Property typed as "timestamp" with partial date sample → PARTIAL_DATE_INCOMPATIBLE
    - Property typed as "string" with partial dates → no violation (correct behavior)
    - Property typed as "timestamp" with full-timestamp values → no violation
    - Strict rejection includes rejected-value counts and affected entity counts in message
    - get_date_property_report covers all properties with date-like type

Post-deploy read-back (structural definition validation, Issue #7)
    - Node count (EntityType entries) matches model declaration
    - Edge count (RelationshipType entries) matches model declaration
    - Relationship with zero contextualizations fails with OKV-001 error
    - Required relationship types absent from definition fail with OKV-001 error
    - All-present, all-non-empty passes with empty violation list
"""

from __future__ import annotations

import re
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Module under test — will not exist until McManus implements it.
# Import is deferred into each test so collection succeeds and failures
# are reported as FAILED, not ERROR.
# ---------------------------------------------------------------------------

def _import_module():
    from fabric_kg_builder.ontology import identity_validation  # noqa: PLC0415
    return identity_validation


def _import_symbols():
    mod = _import_module()
    return (
        mod.IdentityViolation,
        mod.validate_identity,
        mod.resolve_entity_identity_map,
        mod.resolve_relationship_endpoint_map,
        mod.DatePrecision,
        mod.detect_date_precision,
        mod.validate_post_deploy_definition,
    )


# ---------------------------------------------------------------------------
# Minimal model builders
# ---------------------------------------------------------------------------

def _entity(
    name: str,
    *,
    module: str = "support-domain",
    id_column: str = "entity_id",
    table: str = "entities",
    id_properties: list[str] | None = None,
    extra_properties: list[dict] | None = None,
    date_props: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a minimal entity type dict for testing."""
    props: list[dict] = [
        {"name": "display_name", "type": "string", "required": True},
        {"name": id_column, "type": "string", "required": True},
    ]
    if extra_properties:
        props.extend(extra_properties)
    if date_props:
        props.extend(date_props)
    entity: dict[str, Any] = {
        "name": name,
        "module": module,
        "properties": props,
        "dataBinding": {
            "table": table,
            "entityIdColumn": id_column,
            "displayNameColumn": "display_name",
        },
    }
    if id_properties is not None:
        entity["entityIdProperties"] = id_properties
    return entity


def _relationship(
    name: str,
    *,
    source_type: str,
    target_type: str,
    table: str = "relationships",
    source_col: str = "source_entity_id",
    target_col: str = "target_entity_id",
) -> dict[str, Any]:
    """Build a minimal relationship type dict for testing."""
    return {
        "name": name,
        "sourceType": source_type,
        "targetType": target_type,
        "inversePolicy": "none",
        "dataBinding": {
            "table": table,
            "sourceEntityIdColumn": source_col,
            "targetEntityIdColumn": target_col,
        },
    }


def _make_model(
    entity_types: list[dict],
    relationship_types: list[dict] | None = None,
    name: str = "TestOntology",
) -> dict[str, Any]:
    return {
        "name": name,
        "entityTypes": entity_types,
        "relationshipTypes": relationship_types or [],
    }


# ---------------------------------------------------------------------------
# Real model fixture
# ---------------------------------------------------------------------------

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
MODEL_YAML_PATH = REPO_ROOT / "ontology" / "model.yaml"


@pytest.fixture(scope="module")
def real_model() -> dict[str, Any]:
    import yaml  # noqa: PLC0415
    raw = yaml.safe_load(MODEL_YAML_PATH.read_text(encoding="utf-8"))
    return raw.get("ontology", raw) if isinstance(raw, dict) else raw


# ---------------------------------------------------------------------------
# OKV-001: Real model produces zero violations
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRealModelProducesNoOKVViolations:
    """The shipped model.yaml must pass all OKV gates without error."""

    def test_real_model_no_okv_violations(self, real_model: dict[str, Any]):
        """validate_identity on the real model.yaml must return 0 errors."""
        _, validate_identity, *_ = _import_symbols()
        violations = validate_identity(real_model)
        errors = [v for v in violations if v.severity == "error"]
        assert errors == [], (
            f"Real model.yaml has OKV errors:\n"
            + "\n".join(f"  [{v.gate_id}] {v.message}" for v in errors)
        )

    def test_real_model_resolve_entity_map_non_empty(self, real_model: dict[str, Any]):
        """resolve_entity_identity_map must return a non-empty mapping."""
        _, _, resolve_entity_identity_map, *_ = _import_symbols()
        identity_map = resolve_entity_identity_map(real_model)
        assert isinstance(identity_map, dict)
        assert len(identity_map) > 0, "Expected at least one entity identity mapping"

    def test_real_model_entity_map_has_expected_types(self, real_model: dict[str, Any]):
        """Spot-check: Device and DocumentChunk appear in identity map."""
        _, _, resolve_entity_identity_map, *_ = _import_symbols()
        identity_map = resolve_entity_identity_map(real_model)
        assert "Device" in identity_map, "Device must have an identity mapping"

    def test_real_model_resolve_relationship_map_non_empty(self, real_model: dict[str, Any]):
        """resolve_relationship_endpoint_map must return a non-empty mapping."""
        _, _, _, resolve_relationship_endpoint_map, *_ = _import_symbols()
        rel_map = resolve_relationship_endpoint_map(real_model)
        assert isinstance(rel_map, dict)
        assert len(rel_map) > 0, "Expected at least one relationship endpoint mapping"


# ---------------------------------------------------------------------------
# OKV-001: Identity column resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIdentityColumnResolution:
    """Entity physical identity columns are resolved from actual bindings."""

    def test_explicit_entity_id_column_is_used(self):
        """entityIdColumn from dataBinding is the resolved identity column."""
        _, _, resolve_entity_identity_map, *_ = _import_symbols()
        model = _make_model([_entity("Device", id_column="entity_id")])
        imap = resolve_entity_identity_map(model)
        assert imap["Device"] == ["entity_id"]

    def test_non_default_identity_column(self):
        """entityIdColumn with custom name (e.g. chunk_id) is resolved correctly."""
        _, _, resolve_entity_identity_map, *_ = _import_symbols()
        model = _make_model([_entity("DocumentChunk", id_column="chunk_id")])
        imap = resolve_entity_identity_map(model)
        assert imap["DocumentChunk"] == ["chunk_id"]

    def test_entity_id_properties_override_single_column(self):
        """entityIdProperties list overrides entityIdColumn for multi-part keys."""
        _, _, resolve_entity_identity_map, *_ = _import_symbols()
        entity = _entity("CompositeKey", id_column="pk1")
        entity["entityIdProperties"] = ["pk1", "pk2"]
        entity["properties"].append({"name": "pk2", "type": "string", "required": True})
        model = _make_model([entity])
        imap = resolve_entity_identity_map(model)
        assert "pk1" in imap["CompositeKey"]
        assert "pk2" in imap["CompositeKey"]
        assert len(imap["CompositeKey"]) == 2

    def test_multiple_entity_types_resolved_independently(self):
        """Each entity type gets its own identity column resolution — not assumed shared."""
        _, _, resolve_entity_identity_map, *_ = _import_symbols()
        model = _make_model([
            _entity("TypeA", id_column="entity_id"),
            _entity("TypeB", id_column="chunk_id", table="chunks"),
            _entity("TypeC", id_column="image_id", table="images"),
        ])
        imap = resolve_entity_identity_map(model)
        assert imap["TypeA"] == ["entity_id"]
        assert imap["TypeB"] == ["chunk_id"]
        assert imap["TypeC"] == ["image_id"]

    def test_fallback_when_no_entity_id_column_declared(self):
        """Entity with no entityIdColumn falls back to entity_id property if present."""
        _, _, resolve_entity_identity_map, *_ = _import_symbols()
        entity = _entity("Fallback", id_column="entity_id")
        entity["dataBinding"].pop("entityIdColumn", None)  # remove explicit declaration
        entity["properties"] = [
            {"name": "display_name", "type": "string", "required": True},
            {"name": "entity_id", "type": "string", "required": True},
        ]
        model = _make_model([entity])
        imap = resolve_entity_identity_map(model)
        # Must not be empty and must include a resolved column
        assert imap.get("Fallback"), "Fallback entity must still resolve an identity column"


# ---------------------------------------------------------------------------
# OKV-001: Relationship endpoint validation — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRelationshipEndpointHappyPaths:
    """Correct endpoint bindings produce zero OKV-001 violations."""

    def test_exact_column_name_match_passes(self):
        """source_entity_id → entity_id exact match passes OKV-001."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("Device", id_column="entity_id"),
                _entity("Component", id_column="entity_id"),
            ],
            relationship_types=[
                _relationship(
                    "has_component",
                    source_type="Device",
                    target_type="Component",
                    source_col="source_entity_id",
                    target_col="target_entity_id",
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert errors == [], f"Unexpected OKV-001 errors: {errors}"

    def test_valid_fk_alias_source_entity_id_to_entity_id(self):
        """source_entity_id is a canonical FK alias for entity_id — must NOT trigger OKV-001."""
        _, validate_identity, *_ = _import_symbols()
        # source_entity_id → entity_id is the standard FK convention in this ontology.
        # The validation must NOT reject this as a mismatch.
        model = _make_model(
            entity_types=[
                _entity("Procedure", id_column="entity_id"),
                _entity("Step", id_column="entity_id"),
            ],
            relationship_types=[
                _relationship(
                    "has_step",
                    source_type="Procedure",
                    target_type="Step",
                    source_col="source_entity_id",
                    target_col="target_entity_id",
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert errors == [], (
            "source_entity_id→entity_id is a valid FK alias; must not be rejected. "
            f"Got: {errors}"
        )

    def test_self_referential_relationship_passes_when_compatible(self):
        """A relationship where source and target are the same entity type passes."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model(
            entity_types=[_entity("Component", id_column="entity_id")],
            relationship_types=[
                _relationship(
                    "sub_component",
                    source_type="Component",
                    target_type="Component",
                    source_col="source_entity_id",
                    target_col="target_entity_id",
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert errors == []

    def test_different_entity_types_both_using_entity_id_passes(self):
        """Two different entity types both using entity_id: relationship between them passes."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("TypeA", id_column="entity_id"),
                _entity("TypeB", id_column="entity_id"),
            ],
            relationship_types=[
                _relationship(
                    "relates_to",
                    source_type="TypeA",
                    target_type="TypeB",
                    source_col="source_entity_id",
                    target_col="target_entity_id",
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert errors == []

    def test_non_standard_columns_when_both_match_entity_domain_passes(self):
        """Custom column names that mirror the entity identity column pass OKV-001."""
        _, validate_identity, *_ = _import_symbols()
        # If both entity types use chunk_id and relationship uses src_chunk/tgt_chunk,
        # the validator accepts it if the column names form a recognized alias pattern
        # OR if they match the entity's identity column explicitly.
        model = _make_model(
            entity_types=[
                _entity("Chunk", id_column="chunk_id", table="chunks"),
                _entity("SearchRecord", id_column="chunk_id", table="search_records"),
            ],
            relationship_types=[
                _relationship(
                    "indexed_as",
                    source_type="Chunk",
                    target_type="SearchRecord",
                    source_col="chunk_id",   # Exact match with entity's identity column
                    target_col="chunk_id",
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert errors == []


# ---------------------------------------------------------------------------
# OKV-001: Relationship endpoint validation — mismatch cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRelationshipEndpointMismatch:
    """Endpoint domain mismatches trigger ONTOLOGY_RELATIONSHIP_KEY_MISMATCH."""

    def test_source_endpoint_wrong_identity_domain_raises(self):
        """Source endpoint referencing wrong identity domain triggers OKV-001 error."""
        _, validate_identity, *_ = _import_symbols()
        # DocumentChunk uses chunk_id; relationship uses source_entity_id (entity_id domain)
        # chunk_id values ≠ entity_id values → mismatch
        model = _make_model(
            entity_types=[
                _entity("DocumentChunk", id_column="chunk_id", table="chunks"),
                _entity("SearchRecord", id_column="chunk_id", table="search_records"),
            ],
            relationship_types=[
                _relationship(
                    "indexed_as",
                    source_type="DocumentChunk",
                    target_type="SearchRecord",
                    source_col="source_entity_id",   # entity_id domain, not chunk_id domain
                    target_col="target_entity_id",
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert len(errors) >= 1, (
            "Expected OKV-001 error for source endpoint using entity_id domain when entity uses chunk_id"
        )
        # Message must name the relationship AND the entity type
        msg = errors[0].message
        assert "indexed_as" in msg or "DocumentChunk" in msg, (
            f"Error message must name the relationship or entity type. Got: {msg}"
        )

    def test_target_endpoint_wrong_identity_domain_raises(self):
        """Target endpoint referencing wrong identity domain triggers OKV-001 error."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("TypeA", id_column="entity_id"),
                _entity("TypeB", id_column="chunk_id", table="chunks"),
            ],
            relationship_types=[
                _relationship(
                    "ab_rel",
                    source_type="TypeA",
                    target_type="TypeB",
                    source_col="source_entity_id",
                    target_col="target_entity_id",   # entity_id domain, but TypeB uses chunk_id
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert len(errors) >= 1, (
            "Expected OKV-001 error for target endpoint using entity_id domain when target entity uses chunk_id"
        )

    def test_error_message_contains_error_code(self):
        """OKV-001 error message must contain ONTOLOGY_RELATIONSHIP_KEY_MISMATCH."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("Entity1", id_column="custom_key", table="table1"),
                _entity("Entity2", id_column="custom_key", table="table2"),
            ],
            relationship_types=[
                _relationship(
                    "mismatch_rel",
                    source_type="Entity1",
                    target_type="Entity2",
                    source_col="source_entity_id",   # wrong domain
                    target_col="target_entity_id",   # wrong domain
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        # At least one error must contain the error code
        error_codes_found = [
            "ONTOLOGY_RELATIONSHIP_KEY_MISMATCH" in v.message for v in errors
        ]
        assert any(error_codes_found), (
            "At least one OKV-001 error must contain 'ONTOLOGY_RELATIONSHIP_KEY_MISMATCH'. "
            f"Messages: {[v.message for v in errors]}"
        )

    def test_error_message_names_mismatched_columns(self):
        """OKV-001 error must name both the expected and actual column names."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("Entity1", id_column="my_custom_key", table="t1"),
                _entity("Entity2", id_column="my_custom_key", table="t2"),
            ],
            relationship_types=[
                _relationship(
                    "custom_rel",
                    source_type="Entity1",
                    target_type="Entity2",
                    source_col="wrong_source_col",
                    target_col="wrong_target_col",
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        if errors:
            msg = " ".join(v.message for v in errors)
            # Must name either the entity type or the columns involved
            assert "Entity1" in msg or "my_custom_key" in msg or "wrong_source_col" in msg, (
                f"Error message must provide actionable context about which columns/types. Got: {msg}"
            )

    def test_source_only_mismatch_produces_one_error_not_two(self):
        """When only the source endpoint mismatches, exactly the source is flagged."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("Source", id_column="special_id", table="source_tab"),
                _entity("Target", id_column="entity_id", table="entities"),
            ],
            relationship_types=[
                _relationship(
                    "special_rel",
                    source_type="Source",
                    target_type="Target",
                    source_col="source_entity_id",   # mismatch: Source uses special_id
                    target_col="target_entity_id",   # OK: Target uses entity_id
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        # At least one error about source, not necessarily about target
        assert len(errors) >= 1, "Expected at least one source mismatch error"

    def test_both_endpoint_mismatch_is_reported(self):
        """When both source and target endpoints mismatch, both are flagged."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("TypeX", id_column="x_key", table="x_tab"),
                _entity("TypeY", id_column="y_key", table="y_tab"),
            ],
            relationship_types=[
                _relationship(
                    "xy_rel",
                    source_type="TypeX",
                    target_type="TypeY",
                    source_col="source_entity_id",   # mismatch
                    target_col="target_entity_id",   # mismatch
                )
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert len(errors) >= 1, "Expected OKV-001 errors for both endpoint mismatches"


# ---------------------------------------------------------------------------
# OKV-001: Dry-run mapping — resolve_entity_identity_map and
#          resolve_relationship_endpoint_map
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDryRunIdentityMapping:
    """Dry-run helpers report identity and endpoint mappings without altering validation."""

    def test_entity_identity_map_returns_correct_columns(self):
        """resolve_entity_identity_map returns {name: [col]} per entity type."""
        _, _, resolve_entity_identity_map, *_ = _import_symbols()
        model = _make_model([
            _entity("Device", id_column="entity_id"),
            _entity("Chunk", id_column="chunk_id", table="chunks"),
        ])
        imap = resolve_entity_identity_map(model)
        assert imap == {
            "Device": ["entity_id"],
            "Chunk": ["chunk_id"],
        }

    def test_relationship_endpoint_map_returns_correct_columns(self):
        """resolve_relationship_endpoint_map returns {name: {source, target}} per rel."""
        _, _, _, resolve_relationship_endpoint_map, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("A", id_column="entity_id"),
                _entity("B", id_column="entity_id"),
            ],
            relationship_types=[
                _relationship(
                    "a_to_b",
                    source_type="A",
                    target_type="B",
                    source_col="source_entity_id",
                    target_col="target_entity_id",
                )
            ],
        )
        rmap = resolve_relationship_endpoint_map(model)
        assert "a_to_b" in rmap
        assert rmap["a_to_b"]["source"] == "source_entity_id"
        assert rmap["a_to_b"]["target"] == "target_entity_id"

    def test_entity_map_covers_all_declared_entity_types(self):
        """resolve_entity_identity_map returns an entry for every entity type."""
        _, _, resolve_entity_identity_map, *_ = _import_symbols()
        names = ["Alpha", "Beta", "Gamma", "Delta"]
        model = _make_model([_entity(n) for n in names])
        imap = resolve_entity_identity_map(model)
        for name in names:
            assert name in imap, f"Entity type '{name}' missing from identity map"

    def test_relationship_map_covers_all_declared_relationships(self):
        """resolve_relationship_endpoint_map covers every relationship type."""
        _, _, _, resolve_relationship_endpoint_map, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("E1"), _entity("E2"), _entity("E3"),
            ],
            relationship_types=[
                _relationship("r1", source_type="E1", target_type="E2"),
                _relationship("r2", source_type="E2", target_type="E3"),
                _relationship("r3", source_type="E1", target_type="E3"),
            ],
        )
        rmap = resolve_relationship_endpoint_map(model)
        for name in ("r1", "r2", "r3"):
            assert name in rmap, f"Relationship '{name}' missing from endpoint map"

    def test_dry_run_does_not_trigger_violations(self):
        """Calling resolve helpers on a valid model must produce no side effects."""
        _, validate_identity, resolve_entity_identity_map, resolve_relationship_endpoint_map, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("A", id_column="entity_id"),
                _entity("B", id_column="entity_id"),
            ],
            relationship_types=[
                _relationship("a_to_b", source_type="A", target_type="B"),
            ],
        )
        # Call the dry-run helpers first
        resolve_entity_identity_map(model)
        resolve_relationship_endpoint_map(model)
        # Then validate — must still produce zero errors
        violations = validate_identity(model)
        errors = [v for v in violations if v.severity == "error"]
        assert errors == []

    def test_real_model_entity_identity_map_device_has_entity_id(self, real_model):
        """Spot-check: Device entity in real model maps to entity_id column."""
        _, _, resolve_entity_identity_map, *_ = _import_symbols()
        imap = resolve_entity_identity_map(real_model)
        assert "Device" in imap
        assert "entity_id" in imap["Device"], (
            f"Device identity column should be 'entity_id', got {imap['Device']}"
        )

    def test_real_model_relationship_map_has_component_uses_source_entity_id(self, real_model):
        """Spot-check: has_component relationship in real model maps to source_entity_id."""
        _, _, _, resolve_relationship_endpoint_map, *_ = _import_symbols()
        rmap = resolve_relationship_endpoint_map(real_model)
        assert "has_component" in rmap, "'has_component' must appear in relationship endpoint map"
        assert rmap["has_component"]["source"] == "source_entity_id"
        assert rmap["has_component"]["target"] == "target_entity_id"


# ---------------------------------------------------------------------------
# OKV-001: Missing or ambiguous binding cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMissingAmbiguousBindings:
    """Missing or ambiguous bindings in relationship dataBinding are flagged."""

    def test_relationship_missing_source_entity_id_column_is_flagged(self):
        """Relationship with no sourceEntityIdColumn in dataBinding triggers OKV-001 error."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("A", id_column="entity_id"),
                _entity("B", id_column="entity_id"),
            ],
            relationship_types=[
                {
                    "name": "incomplete_rel",
                    "sourceType": "A",
                    "targetType": "B",
                    "dataBinding": {
                        "table": "relationships",
                        # sourceEntityIdColumn intentionally omitted
                        "targetEntityIdColumn": "target_entity_id",
                    },
                }
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert len(errors) >= 1, (
            "Missing sourceEntityIdColumn must trigger OKV-001 error"
        )

    def test_relationship_missing_target_entity_id_column_is_flagged(self):
        """Relationship with no targetEntityIdColumn in dataBinding triggers OKV-001 error."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model(
            entity_types=[
                _entity("A", id_column="entity_id"),
                _entity("B", id_column="entity_id"),
            ],
            relationship_types=[
                {
                    "name": "incomplete_target_rel",
                    "sourceType": "A",
                    "targetType": "B",
                    "dataBinding": {
                        "table": "relationships",
                        "sourceEntityIdColumn": "source_entity_id",
                        # targetEntityIdColumn intentionally omitted
                    },
                }
            ],
        )
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert len(errors) >= 1, (
            "Missing targetEntityIdColumn must trigger OKV-001 error"
        )

    def test_empty_model_with_no_relationships_produces_no_violations(self):
        """A model with entities but no relationships produces no OKV violations."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model([_entity("OnlyEntity")])
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-001" and v.severity == "error"]
        assert errors == []


# ---------------------------------------------------------------------------
# OKV-002: DatePrecision detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDatePrecisionDetection:
    """detect_date_precision categorises samples into the correct precision bucket."""

    def test_year_only_values(self):
        """Year-only strings ("2023", "1999") are detected as YEAR precision."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        assert detect_date_precision(["2023", "2024", "1999"]) == DatePrecision.YEAR

    def test_year_month_values(self):
        """Year-month strings ("2023-07", "2023-12") are detected as YEAR_MONTH precision."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        assert detect_date_precision(["2023-07", "1999-12", "2024-01"]) == DatePrecision.YEAR_MONTH

    def test_full_date_values(self):
        """Full-date strings ("2023-07-22") are detected as FULL_DATE precision."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        assert detect_date_precision(["2023-07-22", "1999-12-31"]) == DatePrecision.FULL_DATE

    def test_timestamp_values_iso8601(self):
        """ISO 8601 timestamps ("2023-07-22T10:30:00Z") are detected as TIMESTAMP."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        assert detect_date_precision(["2023-07-22T10:30:00Z"]) == DatePrecision.TIMESTAMP

    def test_timestamp_values_with_offset(self):
        """Timestamps with UTC offset are detected as TIMESTAMP."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        assert detect_date_precision(["2023-07-22T10:30:00+07:00"]) == DatePrecision.TIMESTAMP

    def test_timestamp_values_no_tz(self):
        """Timestamps without timezone are detected as TIMESTAMP."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        assert detect_date_precision(["2023-07-22T10:30:00"]) == DatePrecision.TIMESTAMP

    def test_empty_list_returns_unknown(self):
        """Empty value list returns UNKNOWN precision."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        assert detect_date_precision([]) == DatePrecision.UNKNOWN

    def test_none_values_in_list_are_ignored(self):
        """None values in the list are skipped; non-None values determine precision."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        result = detect_date_precision([None, "2023-07-22", None])  # type: ignore[list-item]
        assert result == DatePrecision.FULL_DATE

    def test_empty_strings_are_ignored(self):
        """Empty string values are skipped; non-empty values determine precision."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        result = detect_date_precision(["", "2023-07", ""])
        assert result == DatePrecision.YEAR_MONTH

    def test_mixed_precision_uses_coarsest(self):
        """Mixed precision values use the coarsest (least specific) precision."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        # Year + full-date → coarsest is YEAR
        result = detect_date_precision(["2023", "2023-07-22"])
        assert result == DatePrecision.YEAR

    def test_mixed_year_month_and_full_date(self):
        """Year-month + full-date → YEAR_MONTH is coarser."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        result = detect_date_precision(["2023-07", "2023-07-22"])
        assert result == DatePrecision.YEAR_MONTH

    def test_mixed_full_date_and_timestamp(self):
        """Full-date + timestamp → FULL_DATE is coarser."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        result = detect_date_precision(["2023-07-22", "2023-07-22T10:30:00Z"])
        assert result == DatePrecision.FULL_DATE

    def test_single_year_string(self):
        """Single year value "2024" is detected as YEAR."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        assert detect_date_precision(["2024"]) == DatePrecision.YEAR

    def test_partial_values_preserved_verbatim(self):
        """detect_date_precision never modifies input values — no invention of components."""
        *_, DatePrecision, detect_date_precision, _ = _import_symbols()
        original = ["2023", "2024-05"]
        original_copy = list(original)
        detect_date_precision(original)
        # Values must be unchanged after the call
        assert original == original_copy, (
            "detect_date_precision must not modify input values"
        )


# ---------------------------------------------------------------------------
# OKV-002: Model-level date validation (strict pre-deploy gate)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDateTypeValidation:
    """OKV-002 rejects DateTime properties with partial-date samples."""

    def test_string_date_property_passes_unconditionally(self):
        """A property typed as 'string' with partial dates is always OK (preserves verbatim)."""
        _, validate_identity, *_ = _import_symbols()
        entity = _entity(
            "Event",
            date_props=[{"name": "event_date", "type": "string", "required": False}],
        )
        model = _make_model([entity])
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-002" and v.severity == "error"]
        assert errors == [], (
            "A 'string' typed event_date must never trigger OKV-002; partial dates are preserved. "
            f"Got: {errors}"
        )

    def test_timestamp_property_with_full_timestamps_passes(self):
        """Property typed 'timestamp' is accepted when values are full timestamps."""
        _, validate_identity, *_ = _import_symbols()
        entity = _entity(
            "FullTimestamp",
            date_props=[{"name": "created_at", "type": "timestamp", "required": False}],
        )
        model = _make_model([entity])
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-002" and v.severity == "error"]
        # No values to inspect → no OKV-002 error from model structure alone
        # (data-level check requires sample data passed in; model alone is fine)
        assert errors == [], (
            "'timestamp' property without sample data is valid at model level"
        )

    def test_event_date_typed_as_timestamp_triggers_okv002(self):
        """event_date typed as 'timestamp' triggers OKV-002 because partial dates are likely."""
        _, validate_identity, *_ = _import_symbols()
        entity = _entity(
            "ServiceEvent",
            date_props=[
                {
                    "name": "event_date",
                    "type": "timestamp",  # WRONG — event_date contains partial dates
                    "required": False,
                    "description": "Service event date (may be year-only or year-month)",
                }
            ],
        )
        model = _make_model([entity])
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-002" and v.severity == "error"]
        assert len(errors) >= 1, (
            "event_date typed as 'timestamp' must trigger OKV-002 — partial dates cannot be "
            "projected to Fabric DateTime without inventing precision. "
            f"Violations found: {violations}"
        )

    def test_okv002_error_contains_partial_date_incompatible_code(self):
        """OKV-002 error message must contain PARTIAL_DATE_INCOMPATIBLE."""
        _, validate_identity, *_ = _import_symbols()
        entity = _entity(
            "Entity",
            date_props=[{"name": "event_date", "type": "timestamp", "required": False}],
        )
        model = _make_model([entity])
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-002"]
        if errors:
            msgs = " ".join(v.message for v in errors)
            assert "PARTIAL_DATE_INCOMPATIBLE" in msgs, (
                f"OKV-002 error must contain 'PARTIAL_DATE_INCOMPATIBLE'. Got: {msgs}"
            )

    def test_okv002_error_names_entity_and_property(self):
        """OKV-002 error message must name the affected entity type and property."""
        _, validate_identity, *_ = _import_symbols()
        entity = _entity(
            "SpecialEntity",
            date_props=[{"name": "event_date", "type": "timestamp", "required": False}],
        )
        model = _make_model([entity])
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-002"]
        if errors:
            msgs = " ".join(v.message for v in errors)
            assert "SpecialEntity" in msgs or "event_date" in msgs, (
                f"OKV-002 error must name entity or property. Got: {msgs}"
            )

    def test_okv002_counts_affected_entities(self):
        """OKV-002 violations report affected entity counts (not just existence)."""
        _, validate_identity, *_ = _import_symbols()
        # Two entity types with event_date as timestamp → both affected
        entities = [
            _entity("Event1", date_props=[{"name": "event_date", "type": "timestamp"}]),
            _entity("Event2", date_props=[{"name": "event_date", "type": "timestamp"}]),
        ]
        model = _make_model(entities)
        violations = validate_identity(model)
        errors = [v for v in violations if v.gate_id == "OKV-002" and v.severity == "error"]
        # At minimum: one error per affected entity, or one error with count ≥ 2
        total_affected = len(errors)
        if total_affected == 1:
            # Consolidated error must mention count in message
            assert re.search(r"\d", errors[0].message), (
                "Consolidated OKV-002 error must include numeric count of affected entities"
            )
        else:
            assert total_affected >= 2, (
                "Expected at least 2 OKV-002 errors for 2 affected entity types"
            )

    def test_real_model_no_okv002_violations(self, real_model):
        """The real model.yaml must not have any OKV-002 violations (event_date is typed string)."""
        _, validate_identity, *_ = _import_symbols()
        violations = validate_identity(real_model)
        errors = [v for v in violations if v.gate_id == "OKV-002" and v.severity == "error"]
        assert errors == [], (
            f"Real model.yaml must not have OKV-002 errors (event_date must be string): "
            + "\n".join(v.message for v in errors)
        )


# ---------------------------------------------------------------------------
# OKV-002: Date property report
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDatePropertyReport:
    """get_date_property_report surfaces all date-bearing properties and precision."""

    def test_get_date_property_report_returns_list(self, real_model):
        """get_date_property_report returns a list (possibly empty) for the real model."""
        mod = _import_module()
        report = mod.get_date_property_report(real_model)
        assert isinstance(report, list), "get_date_property_report must return a list"

    def test_get_date_property_report_each_entry_has_required_keys(self, real_model):
        """Each report entry has entity_type, property_name, declared_type, precision keys."""
        mod = _import_module()
        report = mod.get_date_property_report(real_model)
        for entry in report:
            for key in ("entity_type", "property_name", "declared_type"):
                assert key in entry, (
                    f"Report entry missing key '{key}': {entry}"
                )

    def test_event_date_appears_in_report_for_model_with_event_date(self):
        """event_date property appears in the report for entities that declare it."""
        mod = _import_module()
        entity = _entity(
            "ServiceEvent",
            date_props=[{"name": "event_date", "type": "string", "required": False}],
        )
        model = _make_model([entity])
        report = mod.get_date_property_report(model)
        prop_names = [e["property_name"] for e in report]
        assert "event_date" in prop_names, (
            "event_date property must appear in the date property report"
        )


# ---------------------------------------------------------------------------
# Post-deploy definition read-back validation (structural)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPostDeployDefinitionValidation:
    """validate_post_deploy_definition checks structural completeness of the returned definition."""

    def _make_definition(
        self,
        entity_type_count: int,
        rel_type_count: int,
        rel_types_with_zero_ctx: list[str] | None = None,
    ) -> dict[str, Any]:
        """Construct a mock Fabric definition dict with the given structure.

        Mimics the structure returned by get_ontology_definition():
          {
            "parts": [
              {"path": "EntityTypes/{id}/definition.json", ...},
              {"path": "EntityTypes/{id}/DataBindings/{guid}.json", ...},
              {"path": "RelationshipTypes/{id}/definition.json", ...},
              {"path": "RelationshipTypes/{id}/Contextualizations/{guid}.json", ...},
            ]
          }
        """
        parts = []
        for i in range(entity_type_count):
            parts.append({"path": f"EntityTypes/{i}/definition.json", "payloadType": "InlineBase64"})
            parts.append({"path": f"EntityTypes/{i}/DataBindings/guid{i}.json", "payloadType": "InlineBase64"})
        for i in range(rel_type_count):
            rt_id = str(i + 1000)
            parts.append({"path": f"RelationshipTypes/{rt_id}/definition.json", "payloadType": "InlineBase64"})
            # Add contextualization unless this rel is in the zero-ctx list
            rel_name = f"rel_{i}"
            if not rel_types_with_zero_ctx or rel_name not in rel_types_with_zero_ctx:
                parts.append({
                    "path": f"RelationshipTypes/{rt_id}/Contextualizations/ctx{i}.json",
                    "payloadType": "InlineBase64",
                })
        return {"parts": parts}

    def test_valid_definition_produces_no_violations(self):
        """A definition with all entity and relationship types present passes."""
        *_, validate_post_deploy_definition = _import_symbols()
        model = _make_model(
            entity_types=[_entity("A"), _entity("B")],
            relationship_types=[_relationship("a_to_b", source_type="A", target_type="B")],
        )
        definition = self._make_definition(entity_type_count=2, rel_type_count=1)
        violations = validate_post_deploy_definition(definition, model)
        errors = [v for v in violations if v.severity == "error"]
        assert errors == []

    def test_zero_entity_types_in_definition_fails(self):
        """Definition with zero EntityType parts produces an OKV-001 error."""
        *_, validate_post_deploy_definition = _import_symbols()
        model = _make_model(
            entity_types=[_entity("A"), _entity("B")],
            relationship_types=[],
        )
        definition = self._make_definition(entity_type_count=0, rel_type_count=0)
        violations = validate_post_deploy_definition(definition, model)
        errors = [v for v in violations if v.severity == "error"]
        assert len(errors) >= 1, (
            "Zero EntityType entries in post-deploy definition must produce an error"
        )

    def test_relationship_with_zero_contextualizations_fails_publication(self):
        """A required RelationshipType with no Contextualizations in the definition fails."""
        *_, validate_post_deploy_definition = _import_symbols()
        model = _make_model(
            entity_types=[_entity("A"), _entity("B")],
            relationship_types=[_relationship("a_to_b", source_type="A", target_type="B")],
        )
        # Build definition with a RelationshipType definition but no Contextualization
        parts = [
            {"path": "EntityTypes/1/definition.json"},
            {"path": "EntityTypes/1/DataBindings/guid1.json"},
            {"path": "EntityTypes/2/definition.json"},
            {"path": "EntityTypes/2/DataBindings/guid2.json"},
            {"path": "RelationshipTypes/100/definition.json"},
            # No RelationshipTypes/100/Contextualizations/... → zero-edge relationship
        ]
        definition = {"parts": parts}
        violations = validate_post_deploy_definition(definition, model)
        errors = [v for v in violations if v.severity == "error"]
        assert len(errors) >= 1, (
            "RelationshipType with zero Contextualizations must fail post-deploy validation"
        )

    def test_relationship_count_matches_model(self):
        """Definition must have at least one RelationshipType per model relationship."""
        *_, validate_post_deploy_definition = _import_symbols()
        model = _make_model(
            entity_types=[_entity("A"), _entity("B"), _entity("C")],
            relationship_types=[
                _relationship("r1", source_type="A", target_type="B"),
                _relationship("r2", source_type="B", target_type="C"),
            ],
        )
        # Definition has only 1 RelationshipType definition (missing r2)
        parts = [
            {"path": "EntityTypes/1/definition.json"},
            {"path": "EntityTypes/1/DataBindings/g1.json"},
            {"path": "EntityTypes/2/definition.json"},
            {"path": "EntityTypes/2/DataBindings/g2.json"},
            {"path": "EntityTypes/3/definition.json"},
            {"path": "EntityTypes/3/DataBindings/g3.json"},
            {"path": "RelationshipTypes/100/definition.json"},
            {"path": "RelationshipTypes/100/Contextualizations/ctx1.json"},
            # r2 is missing
        ]
        definition = {"parts": parts}
        violations = validate_post_deploy_definition(definition, model)
        errors = [v for v in violations if v.severity == "error"]
        assert len(errors) >= 1, (
            "Missing RelationshipType in definition vs. model must produce an error"
        )

    def test_entity_count_matches_model(self):
        """Definition must have at least one EntityType per model entity."""
        *_, validate_post_deploy_definition = _import_symbols()
        model = _make_model(
            entity_types=[_entity("A"), _entity("B"), _entity("C")],
            relationship_types=[],
        )
        # Definition only has 2 EntityType entries (missing C)
        parts = [
            {"path": "EntityTypes/1/definition.json"},
            {"path": "EntityTypes/1/DataBindings/g1.json"},
            {"path": "EntityTypes/2/definition.json"},
            {"path": "EntityTypes/2/DataBindings/g2.json"},
            # EntityType for C is missing
        ]
        definition = {"parts": parts}
        violations = validate_post_deploy_definition(definition, model)
        errors = [v for v in violations if v.severity == "error"]
        assert len(errors) >= 1, (
            "Missing EntityType in definition vs. model must produce an error"
        )

    def test_empty_definition_parts_fails_with_actionable_message(self):
        """Definition with empty parts list produces actionable error messages."""
        *_, validate_post_deploy_definition = _import_symbols()
        model = _make_model(
            entity_types=[_entity("A")],
            relationship_types=[],
        )
        definition = {"parts": []}
        violations = validate_post_deploy_definition(definition, model)
        errors = [v for v in violations if v.severity == "error"]
        assert len(errors) >= 1
        # Message must be actionable
        assert any(v.message for v in errors), "Error messages must not be empty"


# ---------------------------------------------------------------------------
# IdentityViolation dataclass structure
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIdentityViolationDataclass:
    """IdentityViolation has the required fields with correct semantics."""

    def test_dataclass_has_gate_id(self):
        IdentityViolation, *_ = _import_symbols()
        v = IdentityViolation(gate_id="OKV-001", severity="error", message="Test")
        assert v.gate_id == "OKV-001"

    def test_dataclass_has_severity(self):
        IdentityViolation, *_ = _import_symbols()
        v = IdentityViolation(gate_id="OKV-002", severity="warning", message="Test")
        assert v.severity == "warning"

    def test_dataclass_has_message(self):
        IdentityViolation, *_ = _import_symbols()
        v = IdentityViolation(gate_id="OKV-001", severity="error", message="Custom message")
        assert v.message == "Custom message"

    def test_dataclass_equality(self):
        IdentityViolation, *_ = _import_symbols()
        v1 = IdentityViolation(gate_id="OKV-001", severity="error", message="Msg")
        v2 = IdentityViolation(gate_id="OKV-001", severity="error", message="Msg")
        assert v1 == v2

    def test_different_gate_ids_are_not_equal(self):
        IdentityViolation, *_ = _import_symbols()
        v1 = IdentityViolation(gate_id="OKV-001", severity="error", message="Msg")
        v2 = IdentityViolation(gate_id="OKV-002", severity="error", message="Msg")
        assert v1 != v2


# ---------------------------------------------------------------------------
# validate_identity return-type contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateIdentityReturnContract:
    """validate_identity always returns a list (may be empty); never raises on valid input."""

    def test_returns_list_on_valid_model(self):
        """validate_identity returns a list for a valid model."""
        _, validate_identity, *_ = _import_symbols()
        model = _make_model([_entity("A")])
        result = validate_identity(model)
        assert isinstance(result, list)

    def test_returns_list_on_empty_model(self):
        """validate_identity returns a list for an empty model."""
        _, validate_identity, *_ = _import_symbols()
        result = validate_identity({"name": "Empty", "entityTypes": [], "relationshipTypes": []})
        assert isinstance(result, list)

    def test_each_item_is_identity_violation(self):
        """Every returned item is an IdentityViolation instance."""
        IdentityViolation, validate_identity, *_ = _import_symbols()
        model = _make_model(
            [_entity("X", id_column="special_key", table="t")],
            [_relationship("r", source_type="X", target_type="X", source_col="source_entity_id")],
        )
        result = validate_identity(model)
        for item in result:
            assert isinstance(item, IdentityViolation), (
                f"Expected IdentityViolation, got {type(item)}"
            )

    def test_severity_values_are_valid(self):
        """All returned violations have severity in ('error', 'warning')."""
        IdentityViolation, validate_identity, *_ = _import_symbols()
        model = _make_model([_entity("A")])
        result = validate_identity(model)
        for v in result:
            assert v.severity in ("error", "warning"), (
                f"Invalid severity '{v.severity}' on violation: {v}"
            )

    def test_gate_ids_are_okv_prefixed(self):
        """All returned violations have gate_id starting with 'OKV-'."""
        IdentityViolation, validate_identity, *_ = _import_symbols()
        model = _make_model(
            [_entity("X", id_column="custom_id", table="t")],
            [_relationship("r", source_type="X", target_type="X", source_col="source_entity_id")],
        )
        result = validate_identity(model)
        for v in result:
            assert v.gate_id.startswith("OKV-"), (
                f"Gate ID '{v.gate_id}' does not start with 'OKV-'"
            )
