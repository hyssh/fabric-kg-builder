"""Tests for infra/names.py — deterministic Azure resource name generation."""
from __future__ import annotations

import re

import pytest

from fabric_kg_builder.infra.names import (
    make_container_registry_name,
    make_document_intelligence_name,
    make_foundry_name,
    make_identity_name,
    make_monitoring_name,
    make_search_name,
    make_storage_name,
    resolve_resource_name,
    validate_fabric_graph_model_name,
    validate_fabric_identifier_name,
)


# ---------------------------------------------------------------------------
# make_storage_name
# ---------------------------------------------------------------------------

class TestMakeStorageName:
    def test_returns_string(self):
        name = make_storage_name("dev")
        assert isinstance(name, str)

    def test_lowercase_alphanumeric_only(self):
        name = make_storage_name("dev")
        assert re.match(r"^[a-z0-9]+$", name), f"Invalid storage name: {name!r}"

    def test_max_length_24(self):
        name = make_storage_name("dev")
        assert len(name) <= 24

    def test_min_length_3(self):
        name = make_storage_name("dev")
        assert len(name) >= 3

    def test_deterministic(self):
        n1 = make_storage_name("dev")
        n2 = make_storage_name("dev")
        assert n1 == n2

    def test_different_environments_differ(self):
        assert make_storage_name("dev") != make_storage_name("prod")

    def test_different_projects_differ(self):
        assert make_storage_name("dev", "kg") != make_storage_name("dev", "other")

    def test_with_unicode_environment(self):
        name = make_storage_name("tëst")
        assert re.match(r"^[a-z0-9]+$", name)
        assert len(name) <= 24

    def test_long_environment_name(self):
        # Should not exceed max length
        name = make_storage_name("production-environment-name-too-long")
        assert len(name) <= 24


# ---------------------------------------------------------------------------
# make_document_intelligence_name
# ---------------------------------------------------------------------------

class TestMakeDocumentIntelligenceName:
    def test_deterministic(self):
        n1 = make_document_intelligence_name("dev")
        n2 = make_document_intelligence_name("dev")
        assert n1 == n2

    def test_max_length_64(self):
        name = make_document_intelligence_name("dev")
        assert len(name) <= 64

    def test_contains_docintel(self):
        name = make_document_intelligence_name("dev")
        assert "docintel" in name

    def test_different_environments_differ(self):
        assert make_document_intelligence_name("dev") != make_document_intelligence_name("prod")


# ---------------------------------------------------------------------------
# make_foundry_name
# ---------------------------------------------------------------------------

class TestMakeFoundryName:
    def test_deterministic(self):
        n1 = make_foundry_name("dev")
        n2 = make_foundry_name("dev")
        assert n1 == n2

    def test_max_length_64(self):
        assert len(make_foundry_name("dev")) <= 64

    def test_contains_aiservices(self):
        name = make_foundry_name("dev")
        assert "aiservices" in name

    def test_different_environments(self):
        assert make_foundry_name("dev") != make_foundry_name("staging")


# ---------------------------------------------------------------------------
# make_search_name
# ---------------------------------------------------------------------------

class TestMakeSearchName:
    def test_deterministic(self):
        n1 = make_search_name("dev")
        n2 = make_search_name("dev")
        assert n1 == n2

    def test_max_length_60(self):
        assert len(make_search_name("dev")) <= 60

    def test_contains_search(self):
        assert "search" in make_search_name("dev")

    def test_different_environments(self):
        assert make_search_name("dev") != make_search_name("prod")


# ---------------------------------------------------------------------------
# make_container_registry_name
# ---------------------------------------------------------------------------

class TestMakeContainerRegistryName:
    def test_deterministic(self):
        n1 = make_container_registry_name("dev")
        n2 = make_container_registry_name("dev")
        assert n1 == n2

    def test_max_length_50(self):
        assert len(make_container_registry_name("dev")) <= 50

    def test_min_length_5(self):
        assert len(make_container_registry_name("dev")) >= 5

    def test_lowercase_alphanumeric_only(self):
        name = make_container_registry_name("dev")
        assert re.match(r"^[a-z0-9]+$", name), f"Invalid ACR name: {name!r}"


# ---------------------------------------------------------------------------
# make_identity_name
# ---------------------------------------------------------------------------

class TestMakeIdentityName:
    def test_deterministic(self):
        n1 = make_identity_name("dev")
        n2 = make_identity_name("dev")
        assert n1 == n2

    def test_max_length_128(self):
        assert len(make_identity_name("dev")) <= 128

    def test_contains_id(self):
        assert "-id-" in make_identity_name("dev") or "id" in make_identity_name("dev")


# ---------------------------------------------------------------------------
# make_monitoring_name
# ---------------------------------------------------------------------------

class TestMakeMonitoringName:
    def test_deterministic(self):
        n1 = make_monitoring_name("my-resource")
        n2 = make_monitoring_name("my-resource")
        assert n1 == n2

    def test_contains_diag(self):
        name = make_monitoring_name("my-resource")
        assert "diag" in name

    def test_different_resources_differ(self):
        assert make_monitoring_name("rsc1") != make_monitoring_name("rsc2")


# ---------------------------------------------------------------------------
# resolve_resource_name
# ---------------------------------------------------------------------------

class TestResolveResourceName:
    def test_returns_configured_name_if_given(self):
        result = resolve_resource_name("my-custom-name", "storage", "dev")
        assert result == "my-custom-name"

    def test_generates_storage_name_if_none(self):
        result = resolve_resource_name(None, "storage", "dev")
        assert isinstance(result, str)
        assert len(result) > 0
        # Should match a generated storage name
        expected = make_storage_name("dev")
        assert result == expected

    def test_generates_foundry_name(self):
        result = resolve_resource_name(None, "foundry", "dev")
        assert result == make_foundry_name("dev")

    def test_generates_search_name(self):
        result = resolve_resource_name(None, "search", "dev")
        assert result == make_search_name("dev")

    def test_generates_identity_name(self):
        result = resolve_resource_name(None, "identity", "dev")
        assert result == make_identity_name("dev")

    def test_generates_document_intelligence_name(self):
        result = resolve_resource_name(None, "document_intelligence", "dev")
        assert result == make_document_intelligence_name("dev")

    def test_generates_container_registry_name(self):
        result = resolve_resource_name(None, "container_registry", "dev")
        assert result == make_container_registry_name("dev")

    def test_unknown_resource_type_raises(self):
        with pytest.raises(ValueError, match="No name generator"):
            resolve_resource_name(None, "unknown_type", "dev")

    def test_empty_configured_name_generates_default(self):
        # Empty string is falsy → generates
        result = resolve_resource_name("", "storage", "dev")
        expected = make_storage_name("dev")
        assert result == expected


# ---------------------------------------------------------------------------
# validate_fabric_identifier_name
# ---------------------------------------------------------------------------

class TestValidateFabricIdentifierName:
    def test_valid_simple_name(self):
        result = validate_fabric_identifier_name("MyLakehouse", "Lakehouse")
        assert result == "MyLakehouse"

    def test_valid_with_underscores(self):
        result = validate_fabric_identifier_name("My_Lakehouse_001", "Lakehouse")
        assert result == "My_Lakehouse_001"

    def test_valid_with_numbers(self):
        result = validate_fabric_identifier_name("Lakehouse01", "Lakehouse")
        assert result == "Lakehouse01"

    def test_rejects_starting_with_digit(self):
        with pytest.raises(ValueError, match="must begin with a letter"):
            validate_fabric_identifier_name("1Lakehouse", "Lakehouse")

    def test_rejects_hyphens(self):
        with pytest.raises(ValueError):
            validate_fabric_identifier_name("my-lakehouse", "Lakehouse")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError):
            validate_fabric_identifier_name("My Lakehouse", "Lakehouse")

    def test_single_letter_is_valid(self):
        result = validate_fabric_identifier_name("A", "Lakehouse")
        assert result == "A"


# ---------------------------------------------------------------------------
# validate_fabric_graph_model_name
# ---------------------------------------------------------------------------

class TestValidateFabricGraphModelName:
    def test_valid_name(self):
        result = validate_fabric_graph_model_name("My Knowledge Graph")
        assert result == "My Knowledge Graph"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="blank"):
            validate_fabric_graph_model_name("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="blank"):
            validate_fabric_graph_model_name("   ")

    def test_too_long_raises(self):
        with pytest.raises(ValueError, match="256"):
            validate_fabric_graph_model_name("A" * 257)

    def test_control_character_raises(self):
        with pytest.raises(ValueError, match="control characters"):
            validate_fabric_graph_model_name("name\x00thing")

    def test_exactly_256_chars_valid(self):
        result = validate_fabric_graph_model_name("A" * 256)
        assert len(result) == 256

    def test_name_with_unicode(self):
        result = validate_fabric_graph_model_name("知識グラフ")
        assert result == "知識グラフ"
