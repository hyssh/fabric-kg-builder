from __future__ import annotations

import inspect
import json
import tomllib
from pathlib import Path

import pytest

from fabric_kg_builder import __version__
from fabric_kg_builder.app.api import create_app


pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_EXPECTED_VERSION = "0.2.4"


def test_all_release_version_surfaces_match() -> None:
    project = tomllib.loads(
        (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    plugin = json.loads(
        (_ROOT / "plugins" / "fabric-kg" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    marketplace = json.loads(
        (_ROOT / ".github" / "plugin" / "marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    lock_text = (_ROOT / "uv.lock").read_text(encoding="utf-8")
    api_default = inspect.signature(create_app).parameters["version"].default

    assert project["project"]["version"] == _EXPECTED_VERSION
    assert __version__ == _EXPECTED_VERSION
    assert plugin["version"] == _EXPECTED_VERSION
    assert marketplace["metadata"]["version"] == _EXPECTED_VERSION
    assert marketplace["plugins"][0]["version"] == _EXPECTED_VERSION
    assert api_default == _EXPECTED_VERSION
    assert (
        'name = "fabric-kg-builder"\nversion = "0.2.4"\n' in lock_text
    )


def test_api_config_default_version_matches_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fabric_kg_builder.app.config import load_app_config

    monkeypatch.setenv("FABRIC_KG_ENVIRONMENT", "local")
    monkeypatch.setenv("FABRIC_KG_LOCAL_DEV", "true")
    monkeypatch.setenv(
        "FABRIC_KG_QUERY_SCHEMA_MODE",
        "schema1_compatibility",
    )
    monkeypatch.delenv("FABRIC_KG_API_VERSION", raising=False)

    assert load_app_config().version == _EXPECTED_VERSION


def test_schema1_compatibility_descriptor_remains_present() -> None:
    payload = json.loads(
        (_ROOT / "apps" / "api" / "schema1-compatibility.json").read_text(
            encoding="utf-8"
        )
    )

    assert payload == {"schema_mode": "schema1_compatibility"}
