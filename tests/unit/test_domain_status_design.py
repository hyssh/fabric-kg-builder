"""Read-only public status for actual sealed design drafts and legacy contracts."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain.design import (
    evaluate_domain_design, generate_domain_design, save_design_artifact,
)
from fabric_kg_builder.domain.guard import evaluate_domain_guard_status
from tests.unit.test_domain_design import Client, _preflight, _sketch
from tests.unit.test_domain_discovery import DiscoveryClient, _prepared, _run
from tests.unit.test_question_routing import _mixed


@pytest.fixture()
def design_file(tmp_path: Path):
    preflight, _, prepared = _prepared(tmp_path, files=2, paragraphs=2)
    discovery = _run(tmp_path, prepared, DiscoveryClient(false_quote=True))
    sketch = _mixed()
    sketch["question_routes"][-1]["unresolved_answer_requirements"] = ["Confirm the source population."]
    draft = generate_domain_design(preflight, discovery=discovery, client=Client(sketch))
    path = tmp_path / "design-draft.json"
    save_design_artifact(path, draft)
    return path, draft


def test_status_validates_design_v2_and_reports_unapproved_coverage_read_only(design_file, tmp_path, monkeypatch):
    path, draft = design_file
    evaluation = evaluate_domain_design(draft)
    before = {item.relative_to(tmp_path): item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}
    monkeypatch.chdir(tmp_path)
    commands = importlib.import_module("fabric_kg_builder.cli.domain_cmd")
    def forbidden(*args, **kwargs):
        pytest.fail("Design status must not call models, parse sources, compile, or write")
    monkeypatch.setattr(commands, "_build_foundry_client", forbidden)
    monkeypatch.setattr("fabric_kg_builder.domain.design.compile_domain_design", forbidden)
    monkeypatch.setattr("fabric_kg_builder.domain.design.save_design_artifact", forbidden)
    monkeypatch.setattr("fabric_kg_builder.enrichment.schema2_sources.IndexedSourceCorpusReader.read", forbidden)
    result = CliRunner().invoke(cli, ["domain", "status", "--file", str(path)])
    assert result.exit_code == 0, result.output
    assert "artifact kind         : domain.design_draft" in result.output
    assert "artifact version      : 2.0.0" in result.output
    assert "design status         : UNAPPROVED" in result.output
    assert "ready for enrichment : False" in result.output
    assert draft.draft_hash in result.output
    assert evaluation.evaluation_hash in result.output
    assert draft.discovery.run_hash in result.output
    assert draft.discovery.prepared.corpus.corpus_hash in result.output
    count = len(draft.discovery.chunks)
    assert f"accounted_chunks={count}/{count}" in result.output
    assert "declared_sources=2, prepared_sources=2" in result.output
    assert f"quarantined={count}" in result.output
    assert "question context      : UNAPPROVED; routed=1/5, pending_requirements=1" in result.output
    assert evaluation.question_routing.context_hash in result.output
    assert "SQL physical bindings : unresolved=1; execution not verified" in result.output
    assert "answer verification   : not_performed" in result.output
    assert "current source bytes/OCR cache not rechecked" in result.output
    assert "semantic recall       : not claimed" in result.output
    assert "domain evaluate-design" in result.output
    assert "domain compile-design" in result.output
    assert "compiled contract, not this draft" in result.output
    assert "convert-legacy" not in result.output
    after = {item.relative_to(tmp_path): item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}
    assert after == before


@pytest.mark.parametrize("mutation", ["hash", "version", "kind", "sketch", "discovery", "syntax", "incomplete"])
def test_status_invalid_design_fails_without_legacy_fallback(design_file, mutation):
    path, _ = design_file
    raw = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "hash":
        raw["draft_hash"] = "0" * 64
    elif mutation == "version":
        raw["artifact_version"] = "99.0.0"
    elif mutation == "kind":
        raw["artifact_kind"] = "domain.not_a_design"
    elif mutation == "sketch":
        raw["sketch"]["domain_name"] = "Unsealed change"
    elif mutation == "discovery":
        raw["discovery"]["chunks"][0]["raw_response"]["candidates"][0]["label"] = "Unsealed observation"
    elif mutation == "incomplete":
        raw = {"artifact_kind": "domain.design_draft"}
    path.write_text('{"artifact_kind":' if mutation == "syntax" else json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    result = CliRunner().invoke(cli, ["domain", "status", "--file", str(path)])
    assert result.exit_code != 0
    assert "Invalid domain" in result.output
    assert "convert-legacy" not in result.output
    assert "Legacy domain.json" not in result.output
    assert "design status         : UNAPPROVED" not in result.output
    assert path.read_bytes() == before


def test_status_sample_only_design_does_not_claim_full_discovery(tmp_path):
    draft = generate_domain_design(_preflight(tmp_path), client=Client(_sketch()), sample_only=True)
    path = tmp_path / "sample-design.json"
    save_design_artifact(path, draft)
    result = CliRunner().invoke(cli, ["domain", "status", "--file", str(path)])
    assert result.exit_code == 0, result.output
    assert "UNAPPROVED" in result.output
    assert "sample-only compatibility, not full corpus" in result.output
    assert "discovery hash" not in result.output
    assert "convert-legacy" not in result.output


@pytest.mark.parametrize("payload", [[], None, "not a domain artifact"])
def test_status_non_object_json_is_invalid_not_legacy(tmp_path, payload):
    path = tmp_path / "design-draft.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = CliRunner().invoke(cli, ["domain", "status", "--file", str(path)])
    assert result.exit_code != 0
    assert "expected a JSON object" in result.output
    assert "convert-legacy" not in result.output


@pytest.mark.parametrize("format", ["json", "yaml"])
def test_status_existing_domain_inputs_preserve_guard_output(tmp_path, monkeypatch, format):
    path = tmp_path / f"domain.{format}"
    if format == "json":
        path.write_text(json.dumps({"domain_name": "Records", "entity_types": ["Record"]}), encoding="utf-8")
    else:
        from fabric_kg_builder.domain import default_domain_contract, save_domain_contract
        save_domain_contract(default_domain_contract(), path)
    state = tmp_path / "state"
    expected = evaluate_domain_guard_status(str(path), l1_state_root=state)
    commands = importlib.import_module("fabric_kg_builder.cli.domain_cmd")
    guard = Mock(wraps=commands.evaluate_domain_guard_status)
    monkeypatch.setattr(commands, "evaluate_domain_guard_status", guard)
    result = CliRunner().invoke(cli, ["domain", "status", "--file", str(path), "--state-dir", str(state)])
    assert result.exit_code == 0, result.output
    guard.assert_called_once_with(str(path), l1_state_root=state)
    assert result.output == "\n".join([
        f"[domain status] contract path         : {expected.contract_path}",
        f"[domain status] review path           : {expected.review_path}",
        f"[domain status] legacy path           : {expected.legacy_path}",
        f"[domain status] contract hash         : {expected.contract_hash}",
        f"[domain status] deterministic errors : {expected.deterministic_error_count}",
        f"[domain status] deterministic warnings: {expected.deterministic_warning_count}",
        f"[domain status] ready for enrichment : {expected.ready_for_enrichment}",
        *(f"[domain status] note: {message}" for message in expected.messages),
        "",
    ])


def test_root_help_prefers_discovery_bound_design_and_explicit_compatibility():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    start = result.output.index("Default: corpus-first Schema-2 pipeline")
    end = result.output.index("Explicit compatibility only", start)
    default = result.output[start:end]
    assert "domain discover --input" in default
    assert "--discovery <discovery.json>" in default
    assert "domain evaluate-design" in default
    assert "domain compile-design" in default
    assert "domain approve -> enrich --discovery" in default
    assert "init-domain" not in default
    assert "Schema-2 local prototype" not in result.output
    assert "domain design --sample-only" in result.output[end:]
    assert "Legacy semantic-bundle PowerShell example (compatibility only)" in result.output
