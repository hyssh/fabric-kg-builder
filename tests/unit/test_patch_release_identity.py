"""Release identity is separate from historical stage wire versions."""

import importlib.metadata
import json
from pathlib import Path
import re

from click.testing import CliRunner

from fabric_kg_builder import __version__
from fabric_kg_builder.cli import cli
from fabric_kg_builder.version import L7_CONTRACT_RELEASE, RELEASE_VERSION


ROOT = Path(__file__).resolve().parents[2]


def test_patch_version_authorities_and_cli_agree():
    assert __version__ == RELEASE_VERSION == "0.2.8"
    assert L7_CONTRACT_RELEASE == "0.2.4"
    project = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"$', project, flags=re.MULTILINE).group(1) == RELEASE_VERSION
    lock = (ROOT / "uv.lock").read_text()
    assert re.search(r'name = "fabric-kg-builder"\nversion = "([^"]+)"', lock).group(1) == RELEASE_VERSION
    assert importlib.metadata.version("fabric-kg-builder") == RELEASE_VERSION
    plugin = json.loads((ROOT / "plugins/fabric-kg/plugin.json").read_text())
    marketplace = json.loads((ROOT / ".github/plugin/marketplace.json").read_text())
    assert plugin["version"] == marketplace["metadata"]["version"] == RELEASE_VERSION
    assert marketplace["plugins"][0]["version"] == RELEASE_VERSION
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0 and result.output.strip() == f"fabric-kg, version {RELEASE_VERSION}"


def test_existing_optional_native_requirement_is_locked_not_a_new_default():
    project = (ROOT / "pyproject.toml").read_text()
    lock = (ROOT / "uv.lock").read_text()
    assert '"graspologic-native==1.2.5; python_version<\'3.14\'"' in project
    assert 'name = "graspologic-native"\nversion = "1.2.5"' in lock
    assert 'marker = "python_full_version < \'3.14\'"' in lock
    root_package = lock.split('name = "fabric-kg-builder"\n', 1)[1]
    required = root_package.split("[package.optional-dependencies]", 1)[0]
    assert "graspologic-native" not in required
    assert "graphify" not in lock.casefold()
