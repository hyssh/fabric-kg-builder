"""Public window CLI safety boundaries; all models are offline injections."""

import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli


def test_window_commands_registered():
    commands = cli.commands["domain"].commands
    assert {"window-align", "window-status", "window-history",
            "window-schema", "review-window-mapping"} <= commands.keys()


def test_window_schemas_require_no_configuration_or_state(tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("Schemas use no model"))
    result = CliRunner().invoke(cli, ["domain", "window-schema"])
    assert result.exit_code == 0, result.output
    assert {"WindowProposal", "WindowRun", "WindowMappingReview"} <= json.loads(result.output)["schemas"].keys()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("flags", [
    ["--window-mapping", "mapping.json"],
    ["--window-state", "state"],
    ["--window-mapping", "mapping.json", "--window-state", "state"],
    ["--window-mapping", "mapping.json", "--window-state", "state",
     "--discovery", "discovery.json", "--reextract-pending"],
])
def test_mapping_requires_paired_state_and_discovery_and_forbids_new_extraction(tmp_path, monkeypatch, flags):
    from fabric_kg_builder.cli import enrich_cmd

    (tmp_path / "mapping.json").write_text("{}")
    (tmp_path / "discovery.json").write_text("{}")
    (tmp_path / "state").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(enrich_cmd, "_build_foundry_client", lambda *_: pytest.fail("No model allowed"))
    result = CliRunner().invoke(cli, ["enrich", "--input", "source", *flags])
    assert result.exit_code == 2
    assert "window-" in result.output
    assert not (tmp_path / ".fkg").exists()


def test_window_live_rejects_global_dry_run_before_core(tmp_path):
    discovery, seed = tmp_path / "discovery.json", tmp_path / "domain.yaml"
    discovery.write_text("{}")
    seed.write_text("{}")
    state = tmp_path / "windows"
    result = CliRunner().invoke(cli, [
        "--dry-run", "domain", "window-align", "--discovery", str(discovery),
        "--seed-domain", str(seed), "--out-state", str(state), "--live",
    ])
    assert result.exit_code == 2
    assert not state.exists()


def test_window_review_accept_rejects_global_dry_run(tmp_path):
    discovery, seed = tmp_path / "discovery.json", tmp_path / "domain.yaml"
    discovery.write_text("{}")
    seed.write_text("{}")
    result = CliRunner().invoke(cli, [
        "--dry-run", "domain", "review-window-mapping", "--state", str(tmp_path),
        "--discovery", str(discovery), "--target-domain", str(seed),
        "--out", str(tmp_path / "mapping.json"), "--actor", "reviewer",
        "--rationale", "Reviewed schema only", "--accept",
    ])
    assert result.exit_code == 2
    assert not (tmp_path / "mapping.json").exists()
