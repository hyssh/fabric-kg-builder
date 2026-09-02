"""Tests for the project-supplied entity-type profile registry.

The tool used to ship exactly one type profile (``surface-support``) compiled
into ``multitype_plan.py``. These tests pin the replacement contract: profiles
are *project data*, resolved from a file, and their absence is a normal state
that yields "derive types from the observed data" rather than a built-in
fallback vocabulary.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fabric_kg_builder.ontology.multitype_plan import (
    get_type_profile,
    load_type_profiles,
)


@pytest.mark.unit
class TestLoadTypeProfiles:
    def test_missing_registry_is_empty_not_an_error(self, tmp_path: Path) -> None:
        """A project with no profiles is the common case, not a failure."""
        assert load_type_profiles(tmp_path / "absent.yaml") == {}

    def test_reads_profiles_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "type-profiles.yaml"
        path.write_text(
            yaml.safe_dump({"profiles": {"clinical": ["Patient", "Condition"]}}),
            encoding="utf-8",
        )
        assert load_type_profiles(path) == {"clinical": ["Patient", "Condition"]}

    def test_accepts_bare_mapping_without_profiles_key(self, tmp_path: Path) -> None:
        path = tmp_path / "type-profiles.yaml"
        path.write_text(yaml.safe_dump({"legal": ["Contract", "Clause"]}), encoding="utf-8")
        assert load_type_profiles(path) == {"legal": ["Contract", "Clause"]}

    def test_rejects_non_mapping_registry(self, tmp_path: Path) -> None:
        path = tmp_path / "type-profiles.yaml"
        path.write_text(yaml.safe_dump(["Patient"]), encoding="utf-8")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            load_type_profiles(path)

    def test_rejects_profile_that_is_not_a_string_list(self, tmp_path: Path) -> None:
        path = tmp_path / "type-profiles.yaml"
        path.write_text(
            yaml.safe_dump({"profiles": {"bad": [{"name": "Patient"}]}}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="list of type-name strings"):
            load_type_profiles(path)


@pytest.mark.unit
class TestGetTypeProfile:
    def test_resolves_named_profile_from_registry(self, tmp_path: Path) -> None:
        registry = tmp_path / "type-profiles.yaml"
        registry.write_text(
            yaml.safe_dump({"profiles": {"clinical": ["Patient", "Condition"]}}),
            encoding="utf-8",
        )
        assert get_type_profile("clinical", profiles_path=registry) == [
            "Patient",
            "Condition",
        ]

    def test_resolves_a_direct_path_to_a_type_list(self, tmp_path: Path) -> None:
        types_file = tmp_path / "types.yaml"
        types_file.write_text(yaml.safe_dump(["Contract", "Clause"]), encoding="utf-8")
        assert get_type_profile(str(types_file)) == ["Contract", "Clause"]

    def test_resolves_a_direct_path_with_entity_types_key(self, tmp_path: Path) -> None:
        types_file = tmp_path / "types.yaml"
        types_file.write_text(
            yaml.safe_dump({"entity_types": ["Contract"]}), encoding="utf-8"
        )
        assert get_type_profile(str(types_file)) == ["Contract"]

    def test_resolves_a_registry_path_holding_exactly_one_profile(
        self, tmp_path: Path
    ) -> None:
        """The natural thing a user types: point at the registry file itself.

        This case was missed on the first cut and only surfaced by running the
        documented command, so it is pinned here.
        """
        registry = tmp_path / "type-profiles.yaml"
        registry.write_text(
            yaml.safe_dump({"profiles": {"clinical": ["Patient", "Condition"]}}),
            encoding="utf-8",
        )
        assert get_type_profile(str(registry)) == ["Patient", "Condition"]

    def test_registry_path_with_several_profiles_is_rejected_as_ambiguous(
        self, tmp_path: Path
    ) -> None:
        registry = tmp_path / "type-profiles.yaml"
        registry.write_text(
            yaml.safe_dump(
                {"profiles": {"clinical": ["Patient"], "legal": ["Contract"]}}
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError) as exc:
            get_type_profile(str(registry))
        message = str(exc.value)
        assert "ambiguous" in message
        assert "clinical" in message and "legal" in message

    def test_unknown_name_with_no_registry_explains_the_options(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError) as exc:
            get_type_profile("clinical", profiles_path=tmp_path / "absent.yaml")
        message = str(exc.value)
        assert "no profile registry" in message
        assert "omit --type-profile" in message

    def test_unknown_name_with_a_registry_lists_what_is_declared(
        self, tmp_path: Path
    ) -> None:
        registry = tmp_path / "type-profiles.yaml"
        registry.write_text(
            yaml.safe_dump({"profiles": {"clinical": ["Patient"]}}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="clinical"):
            get_type_profile("legal", profiles_path=registry)

    def test_no_profile_is_built_into_the_tool(self, tmp_path: Path) -> None:
        """The regression this whole change exists to prevent.

        With no project registry, *no* profile name may resolve — including the
        one the tool used to ship. A built-in vocabulary would make one domain
        the tool's default while every other domain had to be configured.
        """
        for name in ("surface-support", "default", "core"):
            with pytest.raises(ValueError):
                get_type_profile(name, profiles_path=tmp_path / "absent.yaml")


@pytest.mark.unit
def test_shipped_example_profile_still_loads() -> None:
    """The demoted Surface profile must remain usable as an example."""
    example = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "domains"
        / "surface-support"
        / "type-profiles.yaml"
    )
    profiles = load_type_profiles(example)
    assert "surface-support" in profiles
    assert "Device" in profiles["surface-support"]
