"""CLI boundaries for integrated windows; all inference doubles remain offline."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.cli import domain_window_run_cmd as commands


def test_integrated_commands_registered():
    assert {
        "window-run", "window-run-status", "window-run-history", "window-run-schema",
        "review-window-run-mapping",
    } <= cli.commands["domain"].commands.keys()


@pytest.mark.parametrize("sources", [[], ["--prepared", "prepared.json", "--discovery", "discovery.json"]])
def test_exactly_one_input_required_before_core(tmp_path, monkeypatch, sources):
    for name in ("intake.json", "prepared.json", "discovery.json"):
        (tmp_path / name).write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_core", lambda: pytest.fail("Do not load invalid input"))
    result = CliRunner().invoke(cli, [
        "domain", "window-run", "--intake", "intake.json", "--out-state", "state", *sources,
    ])
    assert result.exit_code == 2
    assert "exactly one" in result.output
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("global_flags,local_flags", [
    (["--dry-run"], ["--live"]),
    ([], ["--live", "--dry-run"]),
])
def test_live_and_dry_run_conflict_before_core(tmp_path, monkeypatch, global_flags, local_flags):
    for name in ("intake.json", "prepared.json"):
        (tmp_path / name).write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_core", lambda: pytest.fail("Do not load conflicting execution"))
    result = CliRunner().invoke(cli, [
        *global_flags, "domain", "window-run", "--prepared", "prepared.json",
        "--intake", "intake.json", "--out-state", "state", *local_flags,
    ])
    assert result.exit_code == 2
    assert "conflicts" in result.output
    assert not (tmp_path / "state").exists()


def test_resume_requires_existing_state(tmp_path, monkeypatch):
    for name in ("intake.json", "prepared.json"):
        (tmp_path / name).write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_core", lambda: pytest.fail("No state to resume"))
    result = CliRunner().invoke(cli, [
        "domain", "window-run", "--prepared", "prepared.json", "--intake", "intake.json",
        "--out-state", "missing", "--resume", "--live",
    ])
    assert result.exit_code == 2
    assert "existing" in result.output


def test_existing_state_requires_explicit_resume(tmp_path, monkeypatch):
    for name in ("intake.json", "prepared.json"):
        (tmp_path / name).write_text("{}")
    state = tmp_path / "state"
    state.mkdir()
    (state / "keep.txt").write_text("keep")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_core", lambda: pytest.fail("Do not replace state"))
    result = CliRunner().invoke(cli, [
        "domain", "window-run", "--prepared", "prepared.json", "--intake", "intake.json",
        "--out-state", "state", "--live",
    ])
    assert result.exit_code == 2
    assert "already exists" in result.output
    assert (state / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("retry_flag", ["--retry-uncertain", "--retry-invalid-response"])
def test_retry_authorizations_require_resume(tmp_path, monkeypatch, retry_flag):
    for name in ("intake.json", "prepared.json"):
        (tmp_path / name).write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_core", lambda: pytest.fail("Retry needs an existing run"))
    result = CliRunner().invoke(cli, [
        "domain", "window-run", "--prepared", "prepared.json", "--intake", "intake.json",
        "--out-state", "state", "--live", retry_flag,
    ])
    assert result.exit_code == 2
    assert "requires --resume" in result.output
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("live", [False, True])
def test_source_overlap_rejected_before_client(tmp_path, monkeypatch, live):
    from fabric_kg_builder.cli import domain_design_cmd
    from tests.unit.test_window_run import inputs

    data = inputs(tmp_path)
    destination = Path(data.prepared.source_path) / "window-state"
    monkeypatch.setattr(domain_design_cmd, "_build_client",
                        lambda *_: pytest.fail("Overlap must fail before model construction"))
    result = CliRunner().invoke(cli, [
        "domain", "window-run", "--prepared", str(tmp_path / "prepared.json"),
        "--intake", str(tmp_path / "intake.json"), "--out-state", str(destination),
        *(["--live"] if live else []),
    ])
    assert result.exit_code == 1
    assert "overlap" in result.output
    assert not destination.exists()


def test_completed_zero_call_resume_and_drift_check_before_client(tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.domain.window_run import RunBudget, run_windowed
    from tests.unit.test_window_run import Client, inputs

    data = inputs(tmp_path)
    state = tmp_path / "state"
    run_windowed(
        inputs=data, output_dir=state, budget=RunBudget(max_calls=100),
        client=Client(), model_version="offline-test",
    )
    before = (state / "run.json").read_bytes()
    monkeypatch.setattr(domain_design_cmd, "_build_client",
                        lambda *_: pytest.fail("Resume must not construct a model"))
    args = [
        "domain", "window-run", "--prepared", str(tmp_path / "prepared.json"),
        "--intake", str(tmp_path / "intake.json"), "--out-state", str(state),
        "--live", "--resume", "--max-calls", "0",
    ]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["model_calls"] == 0
    assert (state / "run.json").read_bytes() == before

    intake = tmp_path / "intake.json"
    changed = json.loads(intake.read_text())
    changed["business_goal"] = "A different business goal"
    intake.write_text(json.dumps(changed))
    result = CliRunner().invoke(cli, [*args[:-1], "1"])
    assert result.exit_code == 1
    assert "binding changed" in result.output
    assert (state / "run.json").read_bytes() == before


def test_zero_window_resume_freezes_the_current_prefix(tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.domain.window_run import RunBudget, run_windowed
    from tests.unit.test_window_run import Client, inputs

    data = inputs(tmp_path)
    state = tmp_path / "state"
    prior = run_windowed(
        inputs=data, output_dir=state, budget=RunBudget(max_calls=100, max_windows=1),
        client=Client(), model_version="offline-test",
    )
    assert prior.state == "partial"
    before = {path: path.read_bytes() for path in (state / "windows").glob("*.json")}
    monkeypatch.setattr(domain_design_cmd, "_build_client",
                        lambda *_: pytest.fail("Freezing a prefix must not build a model"))
    result = CliRunner().invoke(cli, [
        "domain", "window-run", "--prepared", str(tmp_path / "prepared.json"),
        "--intake", str(tmp_path / "intake.json"), "--out-state", str(state),
        "--resume", "--live", "--max-calls", "0", "--max-windows", "0",
    ])
    assert result.exit_code == 0, result.output
    frozen = json.loads(result.output)
    assert frozen["model_calls"] == 0
    assert frozen["result"]["cursor"] == prior.cursor
    assert frozen["result"]["final_snapshot"]["artifact_hash"] == prior.final_snapshot.artifact_hash
    assert before == {path: path.read_bytes() for path in (state / "windows").glob("*.json")}
