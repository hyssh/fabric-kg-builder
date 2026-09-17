"""Compact accepted scope stays source-bound from sealed L4 to native serving."""

import base64
import dataclasses
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.deploy import schema2_prototype as publication
from fabric_kg_builder.deploy import schema2_prototype_agent as agent
from fabric_kg_builder.domain.service import compute_contract_hash, load_domain_contract
from fabric_kg_builder.domain.window_run import load_windowed_run
from fabric_kg_builder.domain.window_run_acceptance import (
    accept_window_run_prefix, prefix_scope_notice,
)
from fabric_kg_builder.knowledge.data_agent import decode_stage_snapshot
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
from fabric_kg_builder.serving import structured_publication
from fabric_kg_builder.serving.structured_publication import (
    L5aPublicationError, _window_run_scope_context, export_serving_question_context,
)
from tests.unit.test_domain_discovery_cli import _invoke
from tests.unit.test_schema2_prototype_agent import WORKSPACE, published
from tests.unit.test_window_run_approved_cli import _approve_integrated, _replay_args, _review
from tests.unit.test_window_run_coverage_acceptance_cli import (
    CoverageModel, _accept as _accept_coverage, partial_case,
)
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case, prefix_command


def _scope(*, prefix=True):
    selected, total = (302, 1425) if prefix else (99, 100)
    return {
        "kind": "domain.window_run_prefix_acceptance" if prefix else "domain.window_run_coverage_acceptance",
        "authority": "limited_committed_prefix_only" if prefix else "prototype_chunk_coverage_only",
        "domain_contract_hash": "b" * 64,
        "acceptance_hash": "a" * 64,
        "scope_notice": prefix_scope_notice(selected, total) if prefix else (
            "PARTIAL WINDOW-RUN COVERAGE: 99 of 100 chunks; one excluded, not observed empty."
        ),
        "selected_chunk_count": selected,
        "total_chunk_count": total,
        "omitted_chunk_count": total - selected,
    }


@pytest.mark.parametrize("prefix", [True, False])
def test_description_is_compact_explicit_and_keeps_full_scope_hash(prefix):
    scope = _scope(prefix=prefix)
    description = publication._prototype_description("f" * 32, scope)
    assert len(description) <= 256
    assert description.startswith("LIMITED PREFIX 302/1425 (1123 excluded)" if prefix else (
        "PARTIAL COVERAGE 99/100 (1 excluded)"
    ))
    assert "NOT full coverage" in description
    assert f"scope={scope['acceptance_hash']}" in description
    if not prefix:
        assert "PREFIX" not in description


def test_normal_description_is_byte_unchanged_and_scoped_description_never_truncates():
    run_id = "f" * 32
    assert publication._prototype_description(run_id).encode() == (
        f"Schema2 create-only prototype run {run_id}; retain partial items"
    ).encode()
    with pytest.raises(publication.PrototypePublicationError, match="no content was truncated"):
        publication._prototype_description("x" * 256, _scope())


@pytest.mark.parametrize("prefix", [True, False])
def test_scope_notice_precedes_generic_instructions_and_remains_in_context(published, prefix):
    handoff = agent._handoff(**{
        key: value for key, value in published.kwargs.items() if key != "out_state"
    })
    scope = _scope(prefix=prefix)
    context = {**handoff.context, "window_run_scope": scope}
    snapshot = decode_stage_snapshot(
        agent._definition(dataclasses.replace(handoff, context=context), "fixture", None), "draft",
    )
    assert snapshot.instruction.startswith(scope["scope_notice"] + "\n\nUse the attached Ontology")
    assert canonical_json(context) in snapshot.instruction
    assert len(snapshot.instruction) <= 15_000
    lakehouse = next(source for source in snapshot.sources if source["type"] == "lakehouse_tables")
    assert json.loads(lakehouse["metadata"]["schema2_question_context"])["window_run_scope"] == scope
    assert published.backend.calls == []
    oversized = {**context, "padding": "x" * 15_000}
    with pytest.raises(agent.Error, match="no content was truncated"):
        agent._definition(dataclasses.replace(handoff, context=oversized), "fixture", None)


@pytest.fixture
def sealed_prefix(prefix_case, prefix_command, tmp_path):
    source, intake, windows = prefix_case
    acceptance = accept_window_run_prefix(
        load_windowed_run(windows), actor="scope-reviewer", rationale="Only the committed prefix is in scope.",
    )
    acceptance_path = tmp_path / "acceptance.json"
    acceptance_path.write_text(canonical_json(acceptance))
    l1, domain, _ = _approve_integrated(
        tmp_path, source, intake, windows, CoverageModel(), acceptance=acceptance_path,
    )
    review, l2 = tmp_path / "review.json", tmp_path / "l2"
    _review(windows, domain, review)
    replay = _invoke(_replay_args(source, windows, domain, l1, l2, review))
    assert replay["l2_model_calls"] == replay["targeted_model_calls"] == 0
    l3, l4 = tmp_path / "l3", tmp_path / "l4"
    for args in [
        ["validate-evidence", "--state", str(l3), "--l2-state", str(l2), "--l1-state", str(l1), "--domain", str(domain)],
        ["project-serving", "--state", str(l4), "--l3-state", str(l3), "--l2-state", str(l2),
         "--l1-state", str(l1), "--domain", str(domain)],
    ]:
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, result.output
    run_root = next(l4.glob("runs/*/stage-receipt.json")).parent
    return SealedL4ServingSource.from_run(run_root, input_manifest_search_roots=(l3,)), l3, load_domain_contract(domain)


def test_genuine_prefix_context_and_compilation_are_bound_and_compact(sealed_prefix, tmp_path, monkeypatch):
    source, l3, contract = sealed_prefix
    context = export_serving_question_context(source)
    scope = context["window_run_scope"]
    acceptance = contract.window_run_acceptance
    assert scope == {
        "kind": acceptance.artifact_kind,
        "authority": "limited_committed_prefix_only",
        "domain_contract_hash": context["domain_contract_hash"],
        "acceptance_hash": acceptance.acceptance_hash,
        "scope_notice": acceptance.scope_notice,
        "selected_chunk_count": 3,
        "total_chunk_count": 100,
        "omitted_chunk_count": 97,
    }
    assert scope["domain_contract_hash"] == compute_contract_hash(contract)
    assert context["approval_status"] == "approved"
    assert context["export_hash"] == canonical_sha256({k: v for k, v in context.items() if k != "export_hash"})
    assert "selected_committed_chunk_ids" not in canonical_json(scope)
    assert "omitted_chunk_ids" not in canonical_json(scope)
    assert all(chunk not in canonical_json(scope) for chunk in acceptance.omitted_chunk_ids)
    compilation = publication._compile(source.root, l3, WORKSPACE, "scope")
    assert compilation.provenance["window_run_scope"] == scope
    description = publication._prototype_description("f" * 32, scope)
    compile_native = publication.compile_fabric_ontology_definition
    compiled_descriptions = []

    def capture_native(*args, **kwargs):
        compiled = compile_native(*args, **kwargs)
        platform = next(part for part in compiled.parts if part["path"] == ".platform")
        assert json.loads(base64.b64decode(platform["payload"]))["metadata"]["description"] == kwargs["description"]
        compiled_descriptions.append(kwargs["description"])
        return compiled

    monkeypatch.setattr(publication, "compile_fabric_ontology_definition", capture_native)
    native = publication._ontology_parts(
        compilation, WORKSPACE, "8dded47d-7f09-4c54-b1b1-22e05e40b901", "Scoped ontology", description,
    )
    assert compiled_descriptions == [description]
    plan_path = tmp_path / "plan.json"
    plan = publication.publish_schema2_prototype(
        l4_run=source.root, l3_root=l3, workspace_id=WORKSPACE, name_prefix="scope",
        plan_path=plan_path, materialize_dir=tmp_path / "materialized",
        journal_path=tmp_path / "journal.json", dry_run=True, approve_live=None,
    )
    assert json.loads(plan_path.read_text())["provenance"]["window_run_scope"] == scope
    assert plan["description"] == publication._prototype_description(plan["run_id"], scope)
    assert compiled_descriptions[-1] == plan["description"]

    class CapturedRequest(Exception):
        pass

    def capture(method, url, **kwargs):
        assert method == "POST"
        assert kwargs["json"]["description"] == plan["description"]
        raise CapturedRequest

    for kind in ("lakehouse", "ontology", "graph"):
        run = SimpleNamespace(plan=plan, data={"actions": {}}, items=lambda: [], save=lambda: None, request=capture)
        with pytest.raises(CapturedRequest):
            publication._Run.create(run, kind, {"parts": native} if kind == "ontology" else None)
    authority = structured_publication._publication_authority

    def mismatched_authority(tables):
        validated_contract, row = authority(tables)
        return validated_contract, {**row, "domain_contract_hash": "0" * 64}

    monkeypatch.setattr(structured_publication, "_publication_authority", mismatched_authority)
    with pytest.raises(L5aPublicationError, match="authority"):
        export_serving_question_context(source)


def test_genuine_coverage_waiver_summary_is_not_mislabeled_prefix(partial_case, tmp_path):
    _, source, intake, _, windows = partial_case
    acceptance_path = tmp_path / "acceptance.json"
    accepted = _accept_coverage(windows, acceptance_path, accept=True)
    assert accepted.exit_code == 0, accepted.output
    _, domain, _ = _approve_integrated(
        tmp_path, source, intake, windows, CoverageModel(), acceptance=acceptance_path,
    )
    contract = load_domain_contract(domain)
    scope = _window_run_scope_context(contract)
    assert scope["kind"] == "domain.window_run_coverage_acceptance"
    assert scope["authority"] == "prototype_chunk_coverage_only"
    assert (scope["selected_chunk_count"], scope["total_chunk_count"], scope["omitted_chunk_count"]) == (99, 100, 1)
    assert scope["acceptance_hash"] == contract.window_run_acceptance.acceptance_hash
    assert "processing-coverage waiver" in scope["scope_notice"]
    assert "not observed empty" in scope["scope_notice"]
    assert not scope["scope_notice"].startswith("LIMITED")


def test_unaccepted_context_preserves_existing_export(published):
    context = export_serving_question_context(published.source)
    assert "window_run_scope" not in context
    assert "window_run_scope" not in published.publication_plan["provenance"]
    assert published.publication_plan["description"] == publication._prototype_description(
        published.publication_plan["run_id"],
    )
