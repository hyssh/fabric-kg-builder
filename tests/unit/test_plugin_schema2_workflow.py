import json
from pathlib import Path

from click.testing import CliRunner

from fabric_kg_builder.cli import cli


ROOT = Path(__file__).resolve().parents[2]


def test_plugin_schema2_operations_are_registered_and_not_legacy_default():
    skill = (ROOT / "plugins/fabric-kg/skills/fabric-kg-pipeline/SKILL.md").read_text()
    for operation in (
        "discover", "design", "evaluate-design", "compile-design", "question-context",
        "window-align", "window-status", "window-history", "review-window-mapping",
        "window-bootstrap", "window-run", "window-run-status", "window-run-history",
        "review-window-run-mapping",
        "accept-window-run-partial",
        "assess", "review-assessment", "revise",
    ):
        assert operation in cli.commands["domain"].commands
        assert f"domain {operation}" in skill
    for command in ("validate-evidence", "project-serving"):
        assert command in cli.commands
        assert command in skill
    assert "Always run `densify`" not in skill
    assert "Design-only" in skill or "design-only" in skill
    assert "NO-GO" in skill
    assert "Legacy" in skill
    assert "Question references may be empty" in skill
    assert "not an approved Schema-2 domain" in skill
    assert "routed to Lakehouse SQL" in skill
    assert "not verified table/column bindings" in skill
    assert "--sample-only" in skill
    assert "enrich --discovery" in skill


def test_machine_contract_discovery_requires_no_configuration_or_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["domain", "assessment-schema"])
    assert result.exit_code == 0, result.output
    bundle = json.loads(result.output)
    assert bundle["contract_version"] == "1.0.0"
    assert bundle["schemas"]["AssessmentReport"]["additionalProperties"] is False
    assert "report_hash" in bundle["schemas"]["AssessmentReport"]["required"]
    assert "owner_local_id" not in bundle["schemas"]["AssessmentReport"]["properties"]
    assert not list(tmp_path.iterdir())
