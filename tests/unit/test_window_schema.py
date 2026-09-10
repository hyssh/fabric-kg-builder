"""Offline working-schema windows reuse immutable discovery observations."""

import copy
import json

import pytest

from fabric_kg_builder.domain.window_schema import (
    DesignReference, WindowBudget, WindowConfig, WorkingConcept,
    load_final_mapping, load_window_run, plan_windows, run_windows, seed_snapshot,
    translate_raw_candidates, window_history, window_status,
)
from tests.unit.test_domain_discovery import DiscoveryClient, _prepared, _run


class ProposalClient:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = responses or []

    def complete_json(self, **request):
        self.requests.append(copy.deepcopy(request))
        if self.responses:
            response = self.responses.pop(0)
            return response(json.loads(request["user"])["input"]) if callable(response) else response
        return {"changes": [], "pending": []}


def _seed(name="Record"):
    return DesignReference(concepts=[
        WorkingConcept(concept_id="type:record", kind="entity", name=name,
                       definition="A source record", identity_policy={"mode": "unresolved"}),
    ])


def _budget(**kwargs):
    return WindowBudget(max_calls=kwargs.get("max_calls", 100),
                        max_tokens=kwargs.get("max_tokens", 10_000_000),
                        max_windows=kwargs.get("max_windows"))


def _alias(payload):
    term = payload["observations"][0]
    return {"changes": [{
        "action": "add_alias", "concept_id": "type:record", "alias": "Open record",
        "observation_ids": [term["observation_id"]], "reason": "Explicit working terminology mapping",
    }], "pending": []}


def test_windows_share_frozen_schema_carry_through_last_chunk_and_remap_previous(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2, paragraphs=4)
    discovery = _run(tmp_path, prepared, max_chunk_chars=128)
    config = WindowConfig(window_size=2)
    client = ProposalClient([{"changes": [], "pending": []}, _alias])
    root = tmp_path / "windows"
    run = run_windows(discovery=discovery, output_dir=root, seed=_seed(), config=config,
                      budget=_budget(), client=client, model_version="fixture")
    assert run.state == "complete"
    plan = plan_windows(discovery, 2)
    assert run.cursor == len(discovery.chunks)
    assert run.last_chunk_id == plan[-1][-1]
    assert len({next(c.chunk.source_file_id for c in discovery.chunks if c.chunk.chunk_id == cid)
                for cid in plan[0]}) == 2
    for index, log in enumerate(run.logs):
        assert log.input_schema_version == index
        assert all(r.schema_hash == log.before_hash and r.schema_version == index for r in log.records)
        assert json.loads(client.requests[index]["user"])["input"]["schema_hash"] == log.before_hash
        if index:
            assert log.before_hash == run.logs[index - 1].after_hash
    assert run.logs[1].newly_mapped_previous_count > 0
    assert all(r.status == "mapped" for r in load_final_mapping(root).records)
    assert all(r.schema_hash == run.final_snapshot.artifact_hash for r in load_final_mapping(root).records)
    assert window_status(root)["cursor"] == len(discovery.chunks)
    assert window_history(root) == run.logs
    untouched = {p: p.read_bytes() for p in (root / "windows").glob("*.json")}
    resumed_client = ProposalClient()
    resumed = run_windows(discovery=discovery, output_dir=root, seed=_seed(), config=config,
                          budget=WindowBudget(), client=resumed_client, model_version="fixture")
    assert resumed.model_call_count == 0 and not resumed_client.requests
    assert resumed.reused_window_count == len(plan)
    assert all(p.read_bytes() == data for p, data in untouched.items())
    assert load_window_run(root).final_mapping_hash == run.final_mapping_hash


def test_missing_budget_partial_then_resume_does_not_replay_completed_windows(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=3)
    discovery = _run(tmp_path, prepared)
    root = tmp_path / "windows"
    config = WindowConfig(window_size=1)
    first = run_windows(discovery=discovery, output_dir=root, config=config)
    assert first.state == "partial" and first.cursor == 0
    assert first.reason == "model_budget_exhausted"
    assert len(load_final_mapping(root).records) == sum(len(c.raw_response["candidates"]) for c in discovery.chunks)
    second = run_windows(discovery=discovery, output_dir=root, config=config,
                         budget=_budget(max_windows=1), client=ProposalClient())
    assert second.state == "partial" and second.cursor == 1
    last = run_windows(discovery=discovery, output_dir=root, config=config,
                       budget=_budget(), client=ProposalClient())
    assert last.state == "complete"
    assert last.model_call_count == len(discovery.chunks) - 1
    assert last.reused_window_count == 1


def test_invalid_proposal_cached_and_never_advances_cursor(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared)
    root = tmp_path / "windows"
    invalid = ProposalClient([{"invented": "approval"}])
    run = run_windows(discovery=discovery, output_dir=root, budget=_budget(), client=invalid)
    assert run.cursor == 0 and "validation_failed" in run.reason
    assert len(list((root / "responses").glob("*.json"))) == 1
    assert not list((root / "windows").glob("*.json"))
    client = ProposalClient()
    resumed = run_windows(discovery=discovery, output_dir=root, budget=WindowBudget(), client=client)
    assert resumed.cursor == 0 and not client.requests
    assert resumed.final_snapshot.version == 0
    repaired = run_windows(discovery=discovery, output_dir=root, budget=_budget(), client=client)
    assert repaired.state == "complete" and repaired.model_call_count == 1
    assert len(list((root / "responses").glob("*.json"))) == 2
    assert load_window_run(root).cursor == len(discovery.chunks)


def test_quarantine_never_supplies_model_facts_or_becomes_mapped(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared, DiscoveryClient(false_quote=True))
    client = ProposalClient()
    root = tmp_path / "windows"
    run = run_windows(discovery=discovery, output_dir=root, seed=_seed("Open record"),
                      budget=_budget(), client=client)
    assert run.state == "complete"
    payload = json.loads(client.requests[0]["user"])["input"]
    assert payload["observations"] == []
    assert payload["eligible_observation_count"] == 0
    assert "Fabricated source statement" not in client.requests[0]["user"]
    assert all(r.status == "quarantined" for r in load_final_mapping(root).records)


def test_unique_normalization_commits_at_boundary_not_during_batch(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2)
    discovery = _run(tmp_path, prepared)
    root = tmp_path / "windows"
    run = run_windows(discovery=discovery, output_dir=root, seed=_seed("OPEN_RECORD"),
                      config=WindowConfig(window_size=1), budget=_budget(), client=ProposalClient())
    assert run.logs[0].records[0].status == "pending"
    assert run.logs[0].decisions[0].review == "deterministic_normalization"
    assert run.logs[1].records[0].status == "mapped"
    assert all(r.status == "mapped" for r in load_final_mapping(root).records)


def test_context_budget_defers_instead_of_silent_truncation(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared)
    client = ProposalClient()
    run = run_windows(discovery=discovery, output_dir=tmp_path / "windows",
                      config=WindowConfig(max_request_chars=1024), budget=_budget(), client=client)
    assert run.cursor == 0 and run.reason == "request_context_budget_deferred"
    assert not client.requests


def test_resume_rejects_changed_seed_config_model_and_source_bytes(tmp_path):
    preflight, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared)
    root = tmp_path / "windows"
    run_windows(discovery=discovery, output_dir=root, seed=_seed())
    for changes in ({"seed": _seed("Other")}, {"config": WindowConfig(window_size=1)},
                    {"model_version": "changed"}):
        with pytest.raises(ValueError, match="binding changed"):
            run_windows(**{"discovery": discovery, "output_dir": root, "seed": _seed(), **changes})
    next(preflight.source_path.glob("*.html")).write_text("<p>Changed source</p>")
    with pytest.raises(ValueError, match="source corpus"):
        run_windows(discovery=discovery, output_dir=root, seed=_seed())
    with pytest.raises(ValueError, match="source corpus"):
        load_window_run(root)


def test_tampered_commit_fails_instead_of_reusing_or_overwriting(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared)
    root = tmp_path / "windows"
    run_windows(discovery=discovery, output_dir=root, budget=_budget(), client=ProposalClient())
    path = root / "windows" / "000000.json"
    raw = json.loads(path.read_text())
    raw["log"]["chunk_ids"] = ["fake"]
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="hash mismatch"):
        load_window_run(root)
    with pytest.raises(ValueError, match="hash mismatch"):
        run_windows(discovery=discovery, output_dir=root)


def test_identity_rewrite_rejected_and_merge_stays_pending(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared)
    def proposal(payload):
        oid = payload["observations"][0]["observation_id"]
        return {"changes": [
            {"action": "add_concept", "concept": {
                "concept_id": "type:record", "kind": "entity", "name": "Open record", "definition": "Rewrite"},
             "observation_ids": [oid], "reason": "Attempt identity overwrite"},
            {"action": "merge", "concept_id": "type:record", "observation_ids": [oid], "reason": "Merge suggestion"},
        ], "pending": []}
    run = run_windows(discovery=discovery, output_dir=tmp_path / "windows", seed=_seed(),
                      budget=_budget(), client=ProposalClient([proposal]))
    assert [d.status for d in run.logs[0].decisions] == ["rejected", "pending"]
    assert run.final_snapshot.concepts == _seed().concepts


def test_translation_preserves_values_references_anchors_and_does_not_mutate(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared)
    root = tmp_path / "windows"
    run_windows(discovery=discovery, output_dir=root, seed=_seed("Open record"),
                budget=_budget(), client=ProposalClient())
    records = [r for r in load_final_mapping(root).records if r.chunk_id == discovery.chunks[0].chunk.chunk_id]
    raw = discovery.chunks[0].response
    before = raw.model_dump(mode="json")
    translated = translate_raw_candidates(raw, records, concept_targets={"type:record": "ApprovedRecord"})
    expected = copy.deepcopy(before)
    expected["candidates"][0]["observed_type"] = "ApprovedRecord"
    assert translated.model_dump(mode="json") == expected
    assert raw.model_dump(mode="json") == before
    assert not translate_raw_candidates(raw, records, concept_targets={}).candidates


def test_reference_constraints_reject_unknown_endpoint_and_conflicting_alias():
    with pytest.raises(ValueError, match="unknown endpoint"):
        seed_snapshot(DesignReference(concepts=[WorkingConcept(
            concept_id="rel:step", kind="relationship", name="has_step", definition="A step relation",
            source_type_ids=["missing"], target_type_ids=["missing"],
        )]))
    with pytest.raises(ValueError, match="conflicting alias"):
        seed_snapshot(DesignReference(concepts=[
            *_seed().concepts, WorkingConcept(concept_id="other", kind="entity", name="Other",
                                            definition="Different", aliases=["Record"]),
        ]))


def test_new_concepts_are_provisional_stable_and_only_visible_after_boundary(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2)
    discovery = _run(tmp_path, prepared)
    def add(payload):
        return {"changes": [{
            "action": "add_concept", "concept": {
                "concept_id": "working:record", "kind": "entity", "name": "Open record",
                "definition": "Observed record category", "identity_policy": {"mode": "unresolved"},
            }, "observation_ids": [payload["observations"][0]["observation_id"]],
            "reason": "Add a provisional observed category without instance identity claims",
        }], "pending": []}
    root = tmp_path / "windows"
    run = run_windows(discovery=discovery, output_dir=root, config=WindowConfig(window_size=1),
                      budget=_budget(), client=ProposalClient([add]))
    assert run.logs[0].records[0].status == "pending"
    assert run.logs[1].records[0].concept_id == "working:record"
    assert run.final_snapshot.provisional_concept_ids == ["working:record"]
    assert all(r.concept_id == "working:record" for r in load_final_mapping(root).records)


class RichDiscoveryClient(DiscoveryClient):
    def __init__(self, direction="source_to_target", property_owner="record"):
        super().__init__()
        self.direction, self.property_owner = direction, property_owner

    def complete_json(self, **request):
        payload = json.loads(request["user"])["input"]
        if "inputs" in payload:
            return super().complete_json(**request)
        anchor = {"span_start": payload["offset_base"],
                  "span_end": payload["offset_base"] + len(payload["text"]),
                  "quote": payload["text"]}
        return {"candidates": [
            {"candidate_kind": "entity", "local_id": "record", "observed_type": "Record",
             "label": "Record", "anchors": [anchor]},
            {"candidate_kind": "entity", "local_id": "subject", "observed_type": "Subject",
             "label": "Subject", "anchors": [anchor]},
            {"candidate_kind": "relationship", "source_local_id": "record", "target_local_id": "subject",
             "observed_predicate": "has_step", "direction": self.direction, "anchor": anchor},
            {"candidate_kind": "property", "owner_local_id": self.property_owner,
             "observed_property": "sequence", "value": 0, "normalized_value": 0, "anchor": anchor},
        ]}


def _rich_seed():
    return DesignReference(concepts=[
        *_seed().concepts,
        WorkingConcept(concept_id="type:subject", kind="entity", name="Subject", definition="Subject"),
        WorkingConcept(concept_id="rel:step", kind="relationship", name="has_step", definition="Has a step",
                       source_type_ids=["type:record"], target_type_ids=["type:subject"],
                       identity_policy={"context_policy": "Within the source record"}),
        WorkingConcept(concept_id="prop:sequence", kind="property", name="sequence",
                       definition="Record sequence", owner_type_ids=["type:record"], value_type="integer"),
    ])


def test_direction_and_owner_constraints_preserve_pending_without_numeric_ban(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared, RichDiscoveryClient(direction="unknown"))
    root = tmp_path / "windows"
    run_windows(discovery=discovery, output_dir=root, seed=_rich_seed(),
                budget=_budget(), client=ProposalClient())
    records = [r for r in load_final_mapping(root).records if r.chunk_id == discovery.chunks[0].chunk.chunk_id]
    assert records[2].status == "pending" and records[2].reason == "direction_requires_review"
    assert records[3].status == "mapped"
    raw = discovery.chunks[0].response
    translated = translate_raw_candidates(raw, records, concept_targets={
        "type:record": "ApprovedRecord", "type:subject": "ApprovedSubject",
        "prop:sequence": "ApprovedSequence", "rel:step": "ApprovedStep",
    })
    assert len(translated.candidates) == 3
    assert translated.candidates[-1].value == translated.candidates[-1].normalized_value == 0
    assert translated.candidates[-1].owner_local_id == raw.candidates[-1].owner_local_id
    assert translated.candidates[-1].anchor == raw.candidates[-1].anchor
    bad = raw.model_dump(mode="json")
    bad["candidates"][-1]["value"] = 999
    with pytest.raises(ValueError, match="evidence/value hash"):
        translate_raw_candidates(type(raw).model_validate(bad), records, concept_targets={
            "type:record": "ApprovedRecord", "prop:sequence": "ApprovedSequence",
        })


def test_wrong_owner_and_endpoint_aliases_are_not_working_approved(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared, RichDiscoveryClient(property_owner="subject"))
    def wrong_alias(payload):
        records = payload["observations"]
        return {"changes": [{
            "action": "add_alias", "concept_id": "prop:sequence", "alias": "sequence",
            "observation_ids": [next(r["observation_id"] for r in records if r["kind"] == "property")],
            "reason": "Wrong owner is not valid evidence",
        }], "pending": []}
    root = tmp_path / "windows"
    run = run_windows(discovery=discovery, output_dir=root, seed=_rich_seed(),
                      budget=_budget(), client=ProposalClient([wrong_alias]))
    assert run.logs[0].decisions[0].status == "rejected"
    assert "owner" in run.logs[0].decisions[0].reason
    assert all(r.status == "pending" for r in load_final_mapping(root).records if r.kind == "property")


def test_failed_report_after_commit_recovers_without_model_calls(tmp_path, monkeypatch):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared)
    from fabric_kg_builder.domain import window_schema
    write = window_schema._write
    def fail_report(path, artifact):
        if path.parent.name == "reports":
            raise OSError("simulated interruption after commit")
        return write(path, artifact)
    root = tmp_path / "windows"
    with monkeypatch.context() as context:
        context.setattr(window_schema, "_write", fail_report)
        with pytest.raises(OSError):
            run_windows(discovery=discovery, output_dir=root, budget=_budget(), client=ProposalClient())
    client = ProposalClient()
    run = run_windows(discovery=discovery, output_dir=root, client=client)
    assert run.state == "complete" and run.model_call_count == 0 and not client.requests
    assert load_window_run(root).cursor == len(discovery.chunks)


def test_cached_response_tamper_and_rehashed_decision_fails_replay(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared)
    root = tmp_path / "windows"
    run = run_windows(discovery=discovery, output_dir=root, budget=_budget(), client=ProposalClient())
    path = root / "responses" / f"{run.logs[0].request_hash}.json"
    raw = json.loads(path.read_text())
    raw["response"]["changes"] = [{"action": "merge"}]
    from fabric_kg_builder.contracts.base import canonical_sha256
    raw["artifact_hash"] = canonical_sha256({k: v for k, v in raw.items() if k != "artifact_hash"})
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_window_run(root)


def test_draft_domain_seed_is_not_treated_as_approved():
    from tests.unit.test_schema2_extraction import _domain
    with pytest.raises(ValueError, match="approved hash-verified"):
        seed_snapshot(_domain())


def test_snapshot_and_nested_definitions_are_immutable():
    snapshot = seed_snapshot(_seed())
    with pytest.raises(TypeError, match="immutable"):
        snapshot.concepts.clear()
    with pytest.raises(TypeError, match="immutable"):
        snapshot.concepts[0].aliases.append("invented")
    with pytest.raises(TypeError, match="immutable"):
        snapshot.concepts[0].identity_policy["mode"] = "invented"


def test_deduplicated_pattern_cards_account_for_every_observation_without_raw_reextract(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=3, paragraphs=8)
    discovery = _run(tmp_path, prepared, max_chunk_chars=128)
    root = tmp_path / "windows"
    client = ProposalClient()
    run = run_windows(discovery=discovery, output_dir=root, seed=_seed(),
                      config=WindowConfig(window_size=128), budget=_budget(), client=client)
    assert run.state == "complete" and len(client.requests) == 1
    payload = json.loads(client.requests[0]["user"])["input"]
    assert len(payload["observations"]) == 1
    card = payload["observations"][0]
    assert card["occurrence_count"] == len(run.logs[0].records)
    assert set(card["observation_ids"]) == {r.observation_id for r in run.logs[0].records}
    assert len(card["context_samples"]) == 2
    assert card["context_coverage"] == "bounded_representatives_only"
    assert all("relative_source_ref" in c and "locator" in c for c in payload["chunks"])
    persisted = json.loads((root / "requests" / f"{run.logs[0].request_hash}.json").read_text())
    assert persisted["request"] == client.requests[0]
    assert load_window_run(root).cursor == len(discovery.chunks)


def test_explicit_deterministic_mode_finishes_without_model_calls_and_retains_unknowns(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=3)
    discovery = _run(tmp_path, prepared)
    root = tmp_path / "windows"
    config = WindowConfig(proposal_mode="deterministic", window_size=1)
    run = run_windows(discovery=discovery, output_dir=root, seed=_seed("OPEN_RECORD"), config=config)
    assert run.state == "complete" and run.cursor == len(discovery.chunks)
    assert run.model_call_count == run.reserved_tokens == 0
    assert run.logs[0].records[0].status == "pending"
    assert run.logs[1].records[0].status == "mapped"
    assert all(log.proposal_mode == "deterministic" for log in run.logs)
    assert load_window_run(root).model_call_count == 0
    assert window_status(root)["mapping_status_counts"] == {"mapped": len(discovery.chunks)}
    resumed = run_windows(discovery=discovery, output_dir=root, seed=_seed("OPEN_RECORD"), config=config)
    assert resumed.model_call_count == 0 and resumed.reused_window_count == len(run.logs)
    unknown_root = tmp_path / "unknown"
    unknown = run_windows(discovery=discovery, output_dir=unknown_root, config=config)
    assert unknown.state == "complete"
    assert all(r.status == "pending" for r in load_final_mapping(unknown_root).records)
    assert window_status(unknown_root)["unresolved_observation_count"] == len(discovery.chunks)
    with pytest.raises(ValueError, match="binding changed"):
        run_windows(discovery=discovery, output_dir=root, seed=_seed("OPEN_RECORD"),
                    config=WindowConfig(window_size=1))
    with pytest.raises(ValueError, match="must not receive a model client"):
        run_windows(discovery=discovery, output_dir=root, config=config, client=ProposalClient())


def test_deterministic_mode_obeys_window_budget_and_resumes_at_exact_cursor(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2)
    discovery = _run(tmp_path, prepared)
    root = tmp_path / "windows"
    config = WindowConfig(proposal_mode="deterministic", window_size=1)
    first = run_windows(discovery=discovery, output_dir=root, config=config,
                        budget=WindowBudget(max_windows=1))
    assert first.state == "partial" and first.cursor == 1
    final = run_windows(discovery=discovery, output_dir=root, config=config)
    assert final.state == "complete" and final.reused_window_count == 1


class FormattingDiscoveryClient(RichDiscoveryClient):
    def complete_json(self, **request):
        response = super().complete_json(**request)
        if "candidates" not in response:
            return response
        for candidate in response["candidates"]:
            for field in ("observed_type", "observed_predicate", "observed_property"):
                if field in candidate:
                    candidate[field] = candidate[field].replace("_", " ").upper()
        return response


def test_last_window_normalizes_dependent_aliases_after_entity_aliases_at_one_boundary(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared, FormattingDiscoveryClient())
    seed = _rich_seed()
    root = tmp_path / "windows"
    run = run_windows(discovery=discovery, output_dir=root, seed=seed,
                      config=WindowConfig(proposal_mode="deterministic", window_size=128))
    assert run.state == "complete" and len(run.logs) == 1
    assert run.final_snapshot.version == 1
    assert all(r.status == "pending" and r.schema_version == 0 for r in run.logs[0].records)
    assert all(d.status == "accepted_working" for d in run.logs[0].decisions)
    records = load_final_mapping(root).records
    assert {r.kind for r in records} == {"entity", "relationship", "property"}
    assert all(r.status == "mapped" and r.schema_version == 1 for r in records)
    assert seed == _rich_seed()
    assert load_window_run(root).last_chunk_id == discovery.chunks[-1].chunk.chunk_id


@pytest.mark.parametrize("declare_subject_property", [False, True])
def test_final_boundary_scopes_same_property_expression_without_blocking_valid_owner(tmp_path, declare_subject_property):
    class BothOwners(FormattingDiscoveryClient):
        def complete_json(self, **request):
            response = super().complete_json(**request)
            if "candidates" in response:
                response["candidates"].append({
                    **response["candidates"][-1], "owner_local_id": "subject",
                })
            return response

    _, _, prepared = _prepared(tmp_path)
    discovery = _run(tmp_path, prepared, BothOwners())
    concepts = list(_rich_seed().concepts)
    if declare_subject_property:
        concepts.append(WorkingConcept(
            concept_id="prop:subject-sequence", kind="property", name="sequence",
            definition="Subject-specific sequence", owner_type_ids=["type:subject"], value_type="integer",
        ))
    root = tmp_path / "windows"
    run = run_windows(discovery=discovery, output_dir=root, seed=DesignReference(concepts=concepts),
                      config=WindowConfig(proposal_mode="deterministic", window_size=128))
    records = load_final_mapping(root).records
    record_fields = [r for r in records if r.kind == "property" and r.owner_type_id == "type:record"]
    subject_fields = [r for r in records if r.kind == "property" and r.owner_type_id == "type:subject"]
    assert record_fields and all(r.concept_id == "prop:sequence" for r in record_fields)
    assert subject_fields
    if declare_subject_property:
        assert all(r.concept_id == "prop:subject-sequence" for r in subject_fields)
    else:
        assert all(r.status == "pending" for r in subject_fields)
        assert any(d.status == "pending" for d in run.logs[0].decisions)
    assert run.final_snapshot.version == 1


@pytest.mark.parametrize("mode", ["deterministic", "model"])
def test_completed_resume_returns_stable_review_authority_and_separate_runtime_counters(tmp_path, mode):
    _, _, prepared = _prepared(tmp_path, files=2)
    discovery = _run(tmp_path, prepared)
    root = tmp_path / "windows"
    config = WindowConfig(proposal_mode=mode, window_size=1)
    first = run_windows(
        discovery=discovery, output_dir=root, seed=_seed("OPEN_RECORD"), config=config,
        budget=_budget(), client=ProposalClient() if mode == "model" else None,
    )
    sealed = load_window_run(root)
    original_bytes = (root / "run.json").read_bytes()
    review_binding = {
        "window_run_hash": sealed.artifact_hash,
        "snapshot_hash": sealed.final_snapshot.artifact_hash,
        "mapping_hash": sealed.final_mapping_hash,
    }
    assert first.artifact_hash == sealed.artifact_hash
    assert first.model_dump(mode="json") == sealed.model_dump(mode="json")
    resumed = run_windows(discovery=discovery, output_dir=root, seed=_seed("OPEN_RECORD"),
                          config=config, budget=WindowBudget())
    assert resumed.model_call_count == resumed.reserved_tokens == 0
    assert resumed.reused_window_count == len(first.logs)
    assert resumed.artifact_hash == first.artifact_hash == review_binding["window_run_hash"]
    assert resumed.final_snapshot.artifact_hash == review_binding["snapshot_hash"]
    assert resumed.final_mapping_hash == review_binding["mapping_hash"]
    assert resumed.model_dump(mode="json") == first.model_dump(mode="json")
    assert resumed.execution_report_hash != first.execution_report_hash
    assert resumed.invocation["reused_window_count"] == len(first.logs)
    assert (root / "run.json").read_bytes() == original_bytes
    assert load_window_run(root).artifact_hash == review_binding["window_run_hash"]
    from fabric_kg_builder.domain.window_schema import WindowRun
    assert WindowRun.model_validate(resumed.model_dump(mode="json")).artifact_hash == sealed.artifact_hash
