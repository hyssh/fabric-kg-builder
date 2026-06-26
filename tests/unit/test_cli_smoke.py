"""Smoke test: CLI entry point and command registry.

Verifies that ``fabric-kg --help`` exits 0 and lists every canonical
command defined in SPEC-001 §7 and the CLI main module.

Run with:  pytest tests/unit/test_cli_smoke.py -v
"""

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli

# ---------------------------------------------------------------------------
# Canonical command names — must match fabric_kg_builder/cli/main.py
# ---------------------------------------------------------------------------

EXPECTED_COMMANDS = [
    "set-domain",
    "inspect-source",
    "enrich",
    "densify",
    "compile-data",
    "compile-ontology",
    "compile-search",
    "package",
    "deploy-lakehouse",
    "deploy-ontology",
    "deploy-search",
    "validate",
    "build-deploy",
    "init",
]


@pytest.mark.unit
class TestCliHelp:
    """CliRunner-based smoke tests for the fabric-kg CLI entry point."""

    def test_help_exits_zero(self):
        """``fabric-kg --help`` must exit 0."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0, (
            f"Expected exit code 0, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

    def test_help_output_is_non_empty(self):
        """``fabric-kg --help`` must produce non-empty output."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.output.strip(), "Expected non-empty help output"

    @pytest.mark.parametrize("command", EXPECTED_COMMANDS)
    def test_command_listed_in_help(self, command: str):
        """Each canonical command name must appear in the --help output."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert command in result.output, (
            f"Command '{command}' not found in fabric-kg --help.\n"
            f"Actual output:\n{result.output}"
        )

    def test_all_expected_commands_registered(self):
        """CLI group must expose every canonical command via ``list_commands``."""
        # Click exposes registered commands via the ``commands`` dict on a Group.
        registered = set(cli.commands.keys())
        missing = set(EXPECTED_COMMANDS) - registered
        assert not missing, (
            f"Commands missing from CLI group: {sorted(missing)}"
        )

    def test_unknown_command_exits_nonzero(self):
        """Invoking an unregistered command must exit non-zero."""
        runner = CliRunner()
        result = runner.invoke(cli, ["not-a-real-command"])
        assert result.exit_code != 0, (
            "Expected non-zero exit code for unknown command, got 0"
        )

    def test_version_option(self):
        """``fabric-kg --version`` must exit 0 and print the version string."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert result.output.strip(), "Expected non-empty version output"

    @pytest.mark.parametrize("command", EXPECTED_COMMANDS)
    def test_each_command_has_help(self, command: str):
        """Each command must respond to --help without error."""
        runner = CliRunner()
        result = runner.invoke(cli, [command, "--help"])
        assert result.exit_code == 0, (
            f"`fabric-kg {command} --help` exited {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )
