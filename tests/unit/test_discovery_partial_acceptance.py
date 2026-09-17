from __future__ import annotations

import copy
import json

import pytest

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.domain.design import (
    compile_domain_design, evaluate_domain_design, generate_domain_design,
    load_domain_design, save_design_artifact,
)
from fabric_kg_builder.domain.discovery import DiscoveryBudget, prepare_discovery_corpus, run_discovery
from fabric_kg_builder.domain.discovery_acceptance import (
    DiscoveryPartialAcceptance, _require_coverage, accept_discovery_partial,
    discovery_partial_design_context, load_discovery_acceptance,
    resolve_discovery_acceptance, save_discovery_acceptance, validate_discovery_acceptance,
)
from tests.unit.test_domain_design import Client, _sketch
from tests.unit.test_domain_discovery import DiscoveryClient, _prepared
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.domain.stage import approve_persisted_l1_draft, finalize_l1_stage


class PartialClient(DiscoveryClient):
    def complete_json(self, **request):
        raw = super().complete_json(**request)
        payload = json.loads(request["user"])["input"]
        if "inputs" in payload:
            return {"summary": "FAILED_SUMMARY_FACT", "covered_input_ids": ["invented"]}
        if "Document 1 record 48" in payload["text"]:
            return {"unexpected": "FAILED_RESPONSE_FACT"}
        candidate = raw["candidates"][0]
        candidate["observed_type"] = "DocumentTwo" if "Document 1" in payload["text"] else "DocumentOne"
        bad = copy.deepcopy(candidate)
        bad["observed_type"] = "QUARANTINED_FACT"
        bad["local_id"] = "bad"
        bad["anchors"][0]["quote"] = "NOT IN THE DOCUMENT"
        raw["candidates"].append(bad)
        return raw


@pytest.fixture
def partial(tmp_path):
    preflight, _, prepared = _prepared(tmp_path, files=2, paragraphs=49)
    run = run_discovery(
        prepared, client=PartialClient(), model_version=preflight.model_version,
        model_hash=preflight.model_hash, cache_dir=tmp_path / "cache",
        budget=DiscoveryBudget(max_calls=110, max_tokens=20_000_000, max_concurrency=1),
    )
    assert len(run.chunks) == 100
    assert sum(item.response is not None for item in run.chunks) == 99
    return preflight, run


def accept(run, **kwargs):
    return accept_discovery_partial(run, actor="user:hyssh", rationale="Prototype coverage acceptance only.", **kwargs)


@pytest.mark.parametrize("accounted,total", [(99, 100), (1415, 1425), (100, 100)])
def test_exact_99_percent_boundary_passes(accounted, total):
    _require_coverage(accounted, total, "0.99")


@pytest.mark.parametrize("accounted,total,minimum", [(9899, 10000, ".99"), (98, 100, ".99"), (99, 100, ".9899"), (1, 0, ".99")])
def test_below_threshold_never_rounds_or_weakens_policy(accounted, total, minimum):
    with pytest.raises(ValueError):
        _require_coverage(accounted, total, minimum)


def test_acceptance_preserves_partial_status_dispositions_and_private_failed_content(partial, tmp_path):
    _, run = partial
    original = canonical_json(run)
    waiver = accept(run)
    assert waiver.accounted_chunks == 99 and waiver.total_chunks == 100
    assert len(waiver.failed_chunk_ids) == 1
    assert len(waiver.per_file_coverage) == 2
    assert sorted(item["accounted_chunks"] for item in waiver.per_file_coverage) == [49, 50]
    assert len(waiver.missing_document_summary_ids) == 2
    assert waiver.grounding["quarantined_candidate_count"] == 99
    assert waiver.failed_summary_metadata
    context = discovery_partial_design_context(run, waiver)
    text = canonical_json(context)
    assert all(value not in text for value in ("QUARANTINED_FACT", "FAILED_RESPONSE_FACT", "FAILED_SUMMARY_FACT"))
    assert "DocumentOne" in text and "DocumentTwo" in text
    assert len(context["per_file_coverage"]) == 2
    assert len(context["document_frontier_previews"]) == 2
    assert context["frontier_root"]["covered_chunk_count"] == 99
    assert run.status == "partial" and not run.full_corpus_design_ready
    assert canonical_json(run) == original
    path = tmp_path / "acceptance.json"
    save_discovery_acceptance(path, waiver)
    before = path.read_bytes()
    save_discovery_acceptance(path, waiver)
    assert load_discovery_acceptance(path) == waiver
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="create-only"):
        save_discovery_acceptance(path, accept_discovery_partial(run, actor="other", rationale="Other review."))


def test_tampered_and_resealed_source_coverage_rejected(partial):
    _, run = partial
    waiver = accept(run)
    assert resolve_discovery_acceptance(waiver.binding, run) == waiver
    with pytest.raises(ValueError, match="binding mismatch"):
        resolve_discovery_acceptance(waiver.binding.model_copy(update={"actor": "changed"}), run)
    value = waiver.model_dump(mode="json")
    value["actor"] = "tampered"
    with pytest.raises(ValueError, match="hash mismatch"):
        DiscoveryPartialAcceptance.model_validate(value)
    value = waiver.model_dump(mode="json")
    next(item for item in value["per_file_coverage"] if item["accounted_chunks"] == 49)["accounted_chunks"] = 50
    value["acceptance_hash"] = canonical_sha256({key: item for key, item in value.items() if key != "acceptance_hash"})
    altered = DiscoveryPartialAcceptance.model_validate(value)
    with pytest.raises(ValueError, match="binding mismatch"):
        validate_discovery_acceptance(altered, run)
    value = waiver.model_dump(mode="json")
    value["discovery_run_hash"] = "f" * 64
    value["acceptance_hash"] = canonical_sha256({key: item for key, item in value.items() if key != "acceptance_hash"})
    with pytest.raises(ValueError, match="another discovery"):
        validate_discovery_acceptance(DiscoveryPartialAcceptance.model_validate(value), run)


def test_source_drift_rejected_and_threshold_cannot_increase_coverage(partial):
    preflight, run = partial
    with pytest.raises(ValueError, match="threshold"):
        accept(run, min_chunk_coverage=1)
    next(preflight.source_path.glob("*.html")).write_text("<p>Changed bytes.</p>")
    with pytest.raises(ValueError):
        accept(run)


def test_context_budget_and_node_binding(partial):
    _, run = partial
    waiver = accept(run)
    with pytest.raises(ValueError, match="prompt bound"):
        discovery_partial_design_context(run, waiver, max_chars=256)
    with pytest.raises(ValueError, match="unknown"):
        discovery_partial_design_context(run, waiver, node_ids=["invented"])
    node = next(item for item in waiver.context_nodes if item.kind == "grounded_chunk")
    context = discovery_partial_design_context(run, waiver, node_ids=[node.node_id])
    assert context["retrieved_nodes"][0]["source_ref"] == node.source_ref


def test_default_stays_strict_and_acceptance_propagates_to_compile(partial, tmp_path, monkeypatch):
    preflight, run = partial
    monkeypatch.setattr(
        "fabric_kg_builder.enrichment.schema2_sources.IndexedSourceCorpusReader.read",
        lambda *args, **kwargs: pytest.fail("Partial acceptance must not reparse successful source units"),
    )
    client = Client(_sketch())
    with pytest.raises(ValueError, match="incomplete"):
        generate_domain_design(preflight, discovery=run, client=client)
    assert not client.calls
    waiver = accept(run)
    draft = generate_domain_design(preflight, discovery=run, discovery_acceptance=waiver, client=client)
    assert draft.artifact_version == "3.0.0"
    assert "all source chunks were processed" not in client.calls[0]["system"]
    assert "FAILED_SUMMARY_FACT" not in client.calls[0]["user"]
    path = tmp_path / "partial-design.json"
    save_design_artifact(path, draft)
    assert load_domain_design(path) == draft
    draft = load_domain_design(path)
    evaluation = evaluate_domain_design(draft)
    assert evaluation.discovery_acceptance == waiver.binding
    compiled = compile_domain_design(draft, evaluation, preflight=preflight)
    assert compiled.proposal.draft_contract.discovery_acceptance == waiver.binding
    assert compiled.proposal.draft_contract.discovery_run_hash == run.run_hash
    assert compiled.proposal.draft_contract.approval.status == "draft"
    state, domain = tmp_path / "l1", tmp_path / "domain.yaml"
    finalize_l1_stage(compiled, decision=None, actor=None, state_root=state, domain_path=domain)
    approve_persisted_l1_draft(
        actor="ontology-reviewer", state_root=state, domain_path=domain,
        expected_project_id=preflight.base_identity.project_id,
        expected_run_id=preflight.run_id, expected_proposal_hash=compiled.proposal.proposal_hash,
    )
    approved = load_domain_contract(domain)
    assert approved.discovery_acceptance == waiver.binding
    assert approved.approval.status == "approved"
    assert approved.approval.approved_by == "ontology-reviewer"
    assert approved.discovery_acceptance.actor == "user:hyssh"


def test_disjoint_available_child_summaries_survive_missing_document_roots(tmp_path):
    class ChildSummaryClient(DiscoveryClient):
        def complete_json(self, **request):
            raw = super().complete_json(**request)
            payload = json.loads(request["user"])["input"]
            if any("summary" in child for child in payload.get("inputs", [])):
                return {"summary": "FAILED_PARENT_FACT", "covered_input_ids": ["invented"]}
            return raw

    preflight, _, prepared = _prepared(tmp_path, files=2, paragraphs=49)
    run = run_discovery(
        prepared, client=ChildSummaryClient(), model_version=preflight.model_version,
        model_hash=preflight.model_hash, cache_dir=tmp_path / "cache",
        budget=DiscoveryBudget(max_calls=200, max_tokens=20_000_000, max_concurrency=1),
    )
    assert run.status == "partial" and not run.document_summaries
    waiver = accept(run)
    summaries = [item for item in waiver.context_nodes if item.kind == "source_summary"]
    assert len(summaries) == 18
    descendants = {item.chunk.chunk_id: {item.chunk.chunk_id} for item in run.chunks}
    for item in run.summaries:
        descendants[item.node_id] = set().union(*(descendants[key] for key in item.child_ids))
    covered = [key for item in summaries for key in descendants[item.source_ref]]
    assert len(covered) == len(set(covered)) == 100
    context = discovery_partial_design_context(run, waiver, node_ids=[summaries[0].node_id])
    assert all(item["summary_excerpts"] for item in context["document_frontier_previews"])
    assert "FAILED_PARENT_FACT" not in canonical_json(context)
    assert context["retrieved_nodes"][0]["summary"] == run.summaries[0].response.summary


def test_empty_typed_response_counts_but_invalid_received_envelope_does_not(tmp_path):
    class EmptyClient(DiscoveryClient):
        def complete_json(self, **request):
            payload = json.loads(request["user"])["input"]
            if "inputs" in payload:
                return {"summary": "Invalid summary.", "covered_input_ids": ["invented"]}
            return {"candidates": []}

    preflight, _, prepared = _prepared(tmp_path, paragraphs=0)
    run = run_discovery(
        prepared, client=EmptyClient(), model_version=preflight.model_version,
        model_hash=preflight.model_hash, cache_dir=tmp_path / "cache",
    )
    assert run.chunks[0].status == "no_candidates" and run.status == "partial"
    assert accept(run).accounted_chunks == 1

    class InvalidClient:
        def complete_json(self, **request):
            return {"unexpected": "Not a candidates array."}

    invalid = run_discovery(
        prepared, client=InvalidClient(), model_version=preflight.model_version,
        model_hash=preflight.model_hash, cache_dir=tmp_path / "invalid-cache",
    )
    assert invalid.chunks[0].raw_response is not None
    with pytest.raises(ValueError, match="0/1"):
        accept(invalid)


def test_unknown_failed_source_denominator_cannot_be_waived(tmp_path, monkeypatch):
    preflight, reader, _ = _prepared(tmp_path, files=2)
    original = reader.read
    failed_id = preflight.corpus.entries[0].source_file_id

    def fail_one(entry):
        if entry.source_file_id == failed_id:
            raise ValueError("Offline parser unavailable")
        return original(entry)

    monkeypatch.setattr(reader, "read", fail_one)
    prepared = prepare_discovery_corpus(preflight, reader=reader)
    run = run_discovery(
        prepared, client=DiscoveryClient(), model_version=preflight.model_version,
        model_hash=preflight.model_hash, cache_dir=tmp_path / "partial-source-cache",
    )
    assert all(item.response is not None for item in run.chunks)
    with pytest.raises(ValueError, match="unknown chunk denominators"):
        accept(run)


@pytest.mark.parametrize("modified", [False, True])
def test_approved_replay_relocates_sealed_waiver_but_checks_actual_source(tmp_path, modified):
    from click.testing import CliRunner
    from fabric_kg_builder.cli import cli
    from fabric_kg_builder.domain.discovery import load_discovery
    from tests.unit.test_discovery_coverage_acceptance_cli import PartialModel
    from tests.unit.test_domain_discovery_cli import _approved, _invoke, _paths

    source, intake, path, _, args = _paths(tmp_path)
    model = PartialModel(fail_chunk=False)
    _invoke([*args, "--live"], model=model)
    run = load_discovery(path)
    waiver = accept(run)
    waiver_path = tmp_path / "acceptance.json"
    save_discovery_acceptance(waiver_path, waiver)
    l1, domain = _approved(tmp_path, source, intake, path, model, discovery_acceptance=waiver_path)
    moved = source.rename(tmp_path / "relocated-corpus")
    assert not source.exists()
    if modified:
        next(moved.glob("*.html")).write_text("<p>Changed after relocation.</p>")
    assert resolve_discovery_acceptance(waiver.binding, run) == waiver
    with pytest.raises(ValueError, match="binding mismatch"):
        resolve_discovery_acceptance(waiver.binding.model_copy(update={"actor": "not-the-reviewer"}), run)
    with pytest.raises((ValueError, FileNotFoundError)):
        accept(run)
    calls = len(model.calls)
    result = CliRunner().invoke(cli, [
        "enrich", "--input", str(moved), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(tmp_path / "l2"),
        "--discovery", str(path), "--replay-only",
    ], obj={"_design_client": model})
    assert len(model.calls) == calls
    if modified:
        assert result.exit_code != 0
        assert "source corpus entries differ from immutable manifest" in result.output
    else:
        assert result.exit_code == 0, (result.output, result.exception)
        report = json.loads(result.output)
        assert report["status"] == "pending_review"
        assert report["l2_model_calls"] == report["targeted_model_calls"] == 0
