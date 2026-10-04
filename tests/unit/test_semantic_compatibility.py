"""Tests for semantic/compatibility.py — contract compatibility classification."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from fabric_kg_builder.semantic.compatibility import (
    CompatibilityChange,
    CompatibilityLevel,
    CompatibilityReport,
    classify_contract_change,
)
from fabric_kg_builder.semantic.models import SemanticContract

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "semantic_contracts"


def _load(path: str) -> SemanticContract:
    with open(_FIXTURE_DIR / path) as f:
        data = yaml.safe_load(f)
    return SemanticContract.model_validate(data)


def _supply_chain() -> SemanticContract:
    return _load("supply-chain.yaml")


def _clinical() -> SemanticContract:
    return _load("clinical-operations.yaml")


# ---------------------------------------------------------------------------
# CompatibilityLevel
# ---------------------------------------------------------------------------


class TestCompatibilityLevel:
    def test_values_are_strings(self) -> None:
        assert CompatibilityLevel.COMPATIBLE == "compatible"
        assert CompatibilityLevel.CONDITIONAL == "conditional"
        assert CompatibilityLevel.BREAKING == "breaking"

    def test_str_enum_behaves_as_string(self) -> None:
        level = CompatibilityLevel.COMPATIBLE
        assert isinstance(level, str)
        assert level.upper() == "COMPATIBLE"


# ---------------------------------------------------------------------------
# classify_contract_change
# ---------------------------------------------------------------------------


class TestClassifyContractChange:
    def test_identical_contracts_compatible(self) -> None:
        contract = _supply_chain()
        report = classify_contract_change(contract, contract)
        assert report.level == CompatibilityLevel.COMPATIBLE
        assert report.changes == []

    def test_report_has_version_info(self) -> None:
        previous = _supply_chain()
        current = _supply_chain()
        report = classify_contract_change(previous, current)
        assert report.previous_version == previous.contract_version
        assert report.current_version == current.contract_version

    def test_report_is_compatibility_report_type(self) -> None:
        contract = _supply_chain()
        report = classify_contract_change(contract, contract)
        assert isinstance(report, CompatibilityReport)

    def test_entity_removed_is_breaking(self) -> None:
        """Remove an entity (and relationships referencing it) -> BREAKING change."""
        previous = _supply_chain()
        current_data = yaml.safe_load((_FIXTURE_DIR / "supply-chain.yaml").read_text())
        # Keep only business-object entity; remove relationships that reference others
        current_data["entity_types"] = [
            et for et in current_data["entity_types"]
            if et["id"] == "entity-type:business-object"
        ]
        current_data["relationship_types"] = []
        current = SemanticContract.model_validate(current_data)
        report = classify_contract_change(previous, current)
        assert report.level == CompatibilityLevel.BREAKING
        assert any(c.code == "ENTITY_REMOVED" for c in report.changes)

    def test_entity_added_is_at_most_conditional(self) -> None:
        """Adding an entity is not BREAKING."""
        previous = _supply_chain()
        current_data = yaml.safe_load((_FIXTURE_DIR / "supply-chain.yaml").read_text())
        # Add a brand new optional entity type
        new_entity = copy.deepcopy(current_data["entity_types"][0])
        new_entity["id"] = "entity-type:new-optional"
        new_entity["name"] = "NewOptional"
        new_entity["parent"] = None
        new_entity["publication_status"] = "optional"
        current_data["entity_types"].append(new_entity)
        current = SemanticContract.model_validate(current_data)
        report = classify_contract_change(previous, current)
        assert report.level != CompatibilityLevel.BREAKING
        assert any(c.code == "ENTITY_ADDED" for c in report.changes)

    def test_relationship_removed_is_breaking(self) -> None:
        """Remove a relationship from current -> BREAKING change."""
        previous = _supply_chain()
        current_data = yaml.safe_load((_FIXTURE_DIR / "supply-chain.yaml").read_text())
        current_data["relationship_types"] = []
        current = SemanticContract.model_validate(current_data)
        report = classify_contract_change(previous, current)
        assert report.level == CompatibilityLevel.BREAKING
        assert any(c.code == "RELATIONSHIP_REMOVED" for c in report.changes)

    def test_cross_domain_contracts_changes_detected(self) -> None:
        """Comparing supply-chain vs clinical should yield changes."""
        sc = _supply_chain()
        cl = _clinical()
        report = classify_contract_change(sc, cl)
        assert len(report.changes) > 0

    def test_change_has_code_path_message_level(self) -> None:
        previous = _supply_chain()
        current_data = yaml.safe_load((_FIXTURE_DIR / "supply-chain.yaml").read_text())
        # Remove a relationship to trigger a BREAKING change
        current_data["relationship_types"] = []
        current = SemanticContract.model_validate(current_data)
        report = classify_contract_change(previous, current)
        for change in report.changes:
            assert isinstance(change, CompatibilityChange)
            assert change.code
            assert change.path
            assert change.message
            assert change.level in list(CompatibilityLevel)

    def test_max_level_selected_correctly(self) -> None:
        """Report level should be the maximum (worst) level in changes."""
        previous = _supply_chain()
        current_data = yaml.safe_load((_FIXTURE_DIR / "supply-chain.yaml").read_text())
        current_data["relationship_types"] = []
        current = SemanticContract.model_validate(current_data)
        report = classify_contract_change(previous, current)
        breaking_changes = [c for c in report.changes if c.level == CompatibilityLevel.BREAKING]
        if breaking_changes:
            assert report.level == CompatibilityLevel.BREAKING

    def test_relationship_added_is_at_most_conditional(self) -> None:
        """Adding a relationship is not BREAKING."""
        previous = _supply_chain()
        current_data = yaml.safe_load((_FIXTURE_DIR / "supply-chain.yaml").read_text())
        # Add a second relationship based on the first
        existing_rel = copy.deepcopy(current_data["relationship_types"][0])
        existing_rel["id"] = "relationship-type:new-optional-rel"
        existing_rel["predicate"] = "new_rel"
        existing_rel["publication_status"] = "optional"
        current_data["relationship_types"].append(existing_rel)
        current = SemanticContract.model_validate(current_data)
        report = classify_contract_change(previous, current)
        assert report.level != CompatibilityLevel.BREAKING
        assert any(c.code == "RELATIONSHIP_ADDED" for c in report.changes)
