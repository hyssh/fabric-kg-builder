import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.cli import enrich_cmd as enrich_module
from tests.unit.test_schema2_stage import _approved_l1


def test_enrich_dry_run_reads_explicit_revision_state_without_writes(tmp_path, monkeypatch):
    l1, domain = _approved_l1(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(cli, [
        "enrich", "--input", str(tmp_path / "source"),
        "--domain-file", str(domain), "--l1-state", str(l1),
        "--l2-state", str(tmp_path / "new-run" / "l2"), "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["status"] == "planned"
    assert report["remote_calls"] == report["writes"] == 0
    assert not (tmp_path / "new-run").exists()
    assert not (tmp_path / "build").exists()
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_explicit_state_is_passed_to_schema2_executor(tmp_path, monkeypatch):
    l1, domain = _approved_l1(tmp_path)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(receipt=SimpleNamespace(stage_receipt_id="receipt:test"))

    monkeypatch.setattr(enrich_module, "_run_schema2_enrichment", execute)
    monkeypatch.chdir(tmp_path)
    l2 = tmp_path / "second-run" / "l2"
    result = CliRunner().invoke(cli, [
        "enrich", "--input", str(tmp_path / "source"),
        "--domain-file", str(domain), "--l1-state", str(l1),
        "--l2-state", str(l2),
    ])
    assert result.exit_code == 0, result.output
    assert calls[0]["l1_state"] == str(l1)
    assert calls[0]["l2_state"] == str(l2)


def test_dry_run_blocks_changed_corpus_and_overlapping_state(tmp_path, monkeypatch):
    l1, domain = _approved_l1(tmp_path)
    monkeypatch.chdir(tmp_path)
    base = [
        "enrich", "--input", str(tmp_path / "source"),
        "--domain-file", str(domain), "--l1-state", str(l1), "--dry-run",
    ]
    runner = CliRunner()
    overlap = runner.invoke(cli, [*base, "--l2-state", str(l1)])
    assert overlap.exit_code != 0
    assert "overlap" in overlap.output
    forced = runner.invoke(cli, [*base, "--l2-state", str(tmp_path / "custom"), "--force"])
    assert forced.exit_code != 0
    assert "cannot delete" in forced.output
    (tmp_path / "source" / "new.html").write_text("<p>New document.</p>", encoding="utf-8")
    changed = runner.invoke(cli, [*base, "--l2-state", str(tmp_path / "custom")])
    assert changed.exit_code != 0
    assert "differ" in changed.output
