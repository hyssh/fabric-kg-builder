"""Concept admission preserves labelled instances and legacy run authority."""

import copy
import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain.concept_policy import (
    CONCEPT_PROMPT_VERSION, CORE_PROMPT_VERSION, REVIEWED_PROMPT_VERSION, COMPACT_REVIEW_PROMPT_VERSION,
    SemanticAdmissionResponse, apply_semantic_admission, concept_metrics, validate_abstraction,
)
from fabric_kg_builder.domain.window_run import (
    RUN_PROMPT_VERSION, RunConfig, load_windowed_run, run_windowed,
)
from tests.unit.test_window_run import Client, budget, inputs, response


ASSESSMENT = {
    "level": "reusable_type",
    "rationale": "Records have a reusable business role independent of a particular document.",
    "reuse_assessment": "No existing class represents governed records.",
}


def concept_response(payload):
    value = response(payload)
    value["candidates"][0]["label"] = payload["text"]
    for proposal in value["schema_proposals"]:
        proposal["abstraction"] = ASSESSMENT
    return value


def core_assessment(kind):
    return {
        **ASSESSMENT, "representation": {"entity": "entity_type", "relationship": "relationship_type",
                                        "property": "property"}[kind],
        "independent_identity_rationale": "Identifiable business record independent of its particular source label.",
    }


def core_response(payload):
    value = concept_response(payload)
    for proposal in value["schema_proposals"]:
        proposal["abstraction"] = core_assessment(proposal["concept"]["kind"])
    return value


def reviewed_response(payload):
    if "repair" not in payload:
        return core_response(payload)
    proposals = []
    for item in payload["repair"]["failed_proposals"]:
        proposal = copy.deepcopy(item["proposal"])
        proposal["replaces_proposal_index"] = item["proposal_index"]
        proposal["reason"] = "Independent review: a record is a reusable identifiable business object."
        proposals.append(proposal)
    return {"candidates": [], "schema_proposals": proposals}


def compact_review_response(payload):
    if "repair" not in payload:
        return core_response(payload)
    return {"decisions": [
        {"proposal_index": item["proposal_index"], "verdict": "admit",
         "reason": "Reusable business role with coherent scope."}
        for item in payload["repair"]["failed_proposals"]
    ]}


@pytest.mark.parametrize("version", [
    CONCEPT_PROMPT_VERSION, CORE_PROMPT_VERSION, REVIEWED_PROMPT_VERSION, COMPACT_REVIEW_PROMPT_VERSION,
])
def test_concept_windows_reuse_class_preserve_instances_and_zero_call_resume(tmp_path, version):
    data = inputs(tmp_path, files=2, paragraphs=3)
    root = tmp_path / "state"
    config = RunConfig(window_size=2, prompt_version=version)
    client = Client({CONCEPT_PROMPT_VERSION: concept_response, CORE_PROMPT_VERSION: core_response,
                     REVIEWED_PROMPT_VERSION: reviewed_response,
                     COMPACT_REVIEW_PROMPT_VERSION: compact_review_response}[version])
    run = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(max_repair_calls=20), client=client)
    assert run.state == "complete"
    assert run.prompt_version == version
    assert [c.name for c in run.final_snapshot.concepts] == ["Record"]
    assert len(run.chunks) > 2
    assert len({item.response.candidates[0].label for item in run.chunks}) > 2
    assert all(r.status == "mapped" for r in run.final_mapping.records)
    metrics = concept_metrics(run.final_snapshot, run.chunks, run.final_mapping.records)
    assert metrics["schema_type_counts"] == {"entity": 1}
    assert metrics["class_instance_name_collisions"] == []
    assert metrics["entity_types"][0]["distinct_instance_labels"] > 2
    for request in client.requests:
        if "decisions" in request["json_schema"]["properties"]:
            assert "Review proposed ontology SCHEMA" in request["system"]
            continue
        assert "CONCEPT-FIRST ONTOLOGY POLICY" in request["system"]
        assert "must use their observed name" not in request["system"]
        assert ("ConceptAbstraction" if version == CONCEPT_PROMPT_VERSION else "CoreConceptAbstraction") in request["json_schema"]["$defs"]
    before = (root / "run.json").read_bytes()
    resumed = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(max_calls=0))
    assert resumed.model_call_count == 0
    assert resumed.final_snapshot == run.final_snapshot
    assert (root / "run.json").read_bytes() == before
    restored = load_windowed_run(root)
    assert restored.artifact_hash == run.run_hash
    from fabric_kg_builder.domain.window_design_context import window_design_context

    context = window_design_context(restored)
    assert context["concept_policy"]["prompt_version"] == version
    assert context["final_snapshot"] == run.final_snapshot.model_dump(mode="json")


@pytest.mark.parametrize("bad", ["missing", "instance", "value", "retrieval_detail", "same_label"])
def test_bad_abstraction_is_not_promoted_and_original_evidence_survives(tmp_path, bad):
    data = inputs(tmp_path, files=1, paragraphs=1)

    def draft(payload):
        value = concept_response(payload)
        if bad == "missing":
            value["schema_proposals"][0].pop("abstraction")
        elif bad == "same_label":
            value["candidates"][0]["label"] = "Record"
        else:
            value["schema_proposals"][0]["abstraction"] = {**ASSESSMENT, "level": bad}
        return value

    run = run_windowed(inputs=data, output_dir=tmp_path / "state",
                       config=RunConfig(prompt_version=CONCEPT_PROMPT_VERSION),
                       budget=budget(), client=Client(draft))
    assert run.reason == "bootstrap_blocked"
    assert not run.final_snapshot.concepts
    assert run.chunks[0].response.candidates
    assert run.logs[0].diagnostics
    assert all(r.status == "pending" for r in run.final_mapping.records)


def test_alias_cannot_turn_an_instance_label_into_a_class_synonym():
    with pytest.raises(ValueError, match="instance labels"):
        validate_abstraction({"action": "add_alias", "alias": "fan"},
                             [{"candidate_kind": "entity", "label": "Fan"}], [0])


def test_subtype_requires_substitutability_rationale():
    proposal = {
        "action": "add_concept", "concept": {"kind": "entity", "name": "City", "parent_type_id": "Country"},
        "abstraction": ASSESSMENT,
    }
    with pytest.raises(ValueError, match="IS-A"):
        validate_abstraction(proposal, [{"candidate_kind": "entity", "label": "Paris"}], [0])


@pytest.mark.parametrize("type_name,labels", [
    ("Part", ["fan", "antenna", "power button"]),
    ("Town", ["Hamlet A", "Hamlet B"]),
    ("Department", ["Claims", "Support"]),
])
def test_admission_is_domain_general_not_a_name_allowlist(type_name, labels):
    validate_abstraction(
        {"action": "add_concept", "concept": {"kind": "entity", "name": type_name},
         "abstraction": ASSESSMENT},
        [{"candidate_kind": "entity", "label": label} for label in labels],
        list(range(len(labels))),
    )


def test_repair_requires_abstraction_without_replacing_instances(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)

    def draft(payload):
        if "repair" in payload:
            proposal = copy.deepcopy(payload["repair"]["failed_proposals"][0]["proposal"])
            proposal["abstraction"] = ASSESSMENT
            proposal["replaces_proposal_index"] = 0
            return {"candidates": [], "schema_proposals": [proposal]}
        value = concept_response(payload)
        value["schema_proposals"][0].pop("abstraction")
        return value

    client = Client(draft)
    run = run_windowed(inputs=data, output_dir=tmp_path / "state",
                       config=RunConfig(prompt_version=CONCEPT_PROMPT_VERSION),
                       budget=budget(max_repair_calls=2), client=client)
    assert [c.name for c in run.final_snapshot.concepts] == ["Record"]
    assert run.repair_call_count
    assert run.logs[0].supersessions
    assert all("CONCEPT-FIRST ONTOLOGY POLICY" in r["system"] for r in client.requests)


def test_cli_defaults_new_runs_to_concepts_and_preserves_explicit_legacy(tmp_path):
    inputs(tmp_path, files=1, paragraphs=1)
    args = [
        "domain", "window-run", "--prepared", str(tmp_path / "prepared.json"),
        "--intake", str(tmp_path / "intake.json"), "--out-state", str(tmp_path / "new-state"),
    ]
    for flags, version in [([], COMPACT_REVIEW_PROMPT_VERSION), (["--schema-policy", "concepts"], CORE_PROMPT_VERSION),
                           (["--schema-policy", "observed-terms"], RUN_PROMPT_VERSION)]:
        result = CliRunner().invoke(cli, [*args, *flags])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["result"]["config"]["prompt_version"] == version
    assert not (tmp_path / "new-state").exists()


def test_cli_resume_cannot_change_policy(tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd

    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "state"
    run_windowed(inputs=data, output_dir=root, budget=budget(), client=Client())
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("No inference on drift"))
    args = [
        "domain", "window-run", "--prepared", str(tmp_path / "prepared.json"),
        "--intake", str(tmp_path / "intake.json"), "--out-state", str(root),
        "--resume", "--live", "--max-calls", "0",
    ]
    before = (root / "run.json").read_bytes()
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(cli, [*args, "--schema-policy", "concepts"])
    assert result.exit_code == 1
    assert "binding changed" in result.output
    assert (root / "run.json").read_bytes() == before


@pytest.mark.parametrize("names,labels,predicate", [
    (["Model", "SKU", "Part"], ["M1", "S1", "fan"], "contains"),
    (["Country", "City", "Town"], ["A", "B", "C"], "contains"),
])
@pytest.mark.parametrize("version", [
    CONCEPT_PROMPT_VERSION, CORE_PROMPT_VERSION, REVIEWED_PROMPT_VERSION, COMPACT_REVIEW_PROMPT_VERSION,
])
def test_concept_chains_use_typed_relations_and_preserve_grounded_local_endpoints(tmp_path, names, labels, predicate, version):
    from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
    from fabric_kg_builder.domain.discovery import prepare_discovery_corpus
    from fabric_kg_builder.domain.stage import preflight_l1_inputs
    from fabric_kg_builder.domain.window_run import load_window_inputs
    from fabric_kg_builder.sources.preparation import indexed_corpus_reader
    from tests.unit.test_l1_stage import _intake

    text = (f"{names[0]} {labels[0]} {predicate} {names[1]} {labels[1]}. "
            f"{names[1]} {labels[1]} {predicate} {names[2]} {labels[2]}.")
    source = tmp_path / "source"
    source.mkdir()
    (source / "chain.html").write_text(f"<html><body><p>{text}</p></body></html>")
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(_intake()))
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=_intake(), project_id="project:concepts", run_id="run:concepts",
        model_version="fixture/1.0.0", model_hash=canonical_sha256({"fixture": "chain"}))
    reader = indexed_corpus_reader(preflight.corpus, source, project_id=preflight.base_identity.project_id)
    prepared = prepare_discovery_corpus(preflight, reader=reader, cache_dir=tmp_path / "cache")
    path = tmp_path / "prepared.json"
    path.write_text(canonical_json(prepared))
    data = load_window_inputs(prepared_path=path, intake_path=intake)

    def draft(payload):
        if "repair" in payload:
            return compact_review_response(payload) if version == COMPACT_REVIEW_PROMPT_VERSION else reviewed_response(payload)
        start = payload["offset_base"]
        anchor = {"span_start": start, "span_end": start + len(payload["text"]), "quote": payload["text"]}
        candidates, proposals = [], []
        for i, (name, label) in enumerate(zip(names, labels)):
            candidates.append({"candidate_kind": "entity", "local_id": str(i), "observed_type": name,
                               "label": label, "anchors": [anchor]})
            proposals.append({
                "action": "add_concept", "candidate_indices": [i], "reason": "Distinct reusable role",
                "abstraction": ASSESSMENT, "concept": {
                    "concept_id": f"type:{i}", "kind": "entity", "name": name,
                    "definition": f"A reusable {name} business role.", "identity_policy": {"mode": "unresolved"}},
            })
        for i in range(2):
            candidates.append({
                "candidate_kind": "relationship", "source_local_id": str(i), "target_local_id": str(i + 1),
                "observed_predicate": predicate, "direction": "source_to_target", "anchor": anchor})
            proposals.append({
                "action": "add_concept", "candidate_indices": [i + 3], "reason": "Explicit contained item",
                "abstraction": ASSESSMENT, "concept": {
                    "concept_id": f"rel:{i}", "kind": "relationship", "name": predicate,
                    "definition": "A typed containment relation.", "source_type_ids": [f"type:{i}"],
                    "target_type_ids": [f"type:{i + 1}"], "identity_policy": {"context_policy": "source statement"}},
            })
        if version != CONCEPT_PROMPT_VERSION:
            for proposal in proposals:
                proposal["abstraction"] = core_assessment(proposal["concept"]["kind"])
        return {"candidates": candidates, "schema_proposals": proposals}

    root = tmp_path / "state"
    run = run_windowed(inputs=data, output_dir=root, budget=budget(max_repair_calls=20), client=Client(draft),
                       config=RunConfig(prompt_version=version))
    assert run.state == "complete"
    assert len(run.final_snapshot.concepts) == 5
    assert all(c.parent_type_id is None for c in run.final_snapshot.concepts)
    assert all(r.status == "mapped" for r in run.final_mapping.records)
    assert {c.label for c in run.chunks[0].response.candidates if c.candidate_kind == "entity"} == set(labels)
    assert load_windowed_run(root).run_hash == run.run_hash


@pytest.mark.parametrize("assessment", [
    {**core_assessment("entity"), "representation": "scalar_value"},
    {**core_assessment("entity"), "representation": "specialized_instance"},
    {**core_assessment("entity"), "reuse_existing_concept_id": "existing:record"},
    {**core_assessment("entity"), "independent_identity_rationale": None},
])
def test_core_policy_rejects_declared_value_specialization_and_existing_fit(tmp_path, assessment):
    data = inputs(tmp_path, files=1, paragraphs=1)

    def draft(payload):
        value = core_response(payload)
        value["schema_proposals"][0]["abstraction"] = assessment
        return value

    run = run_windowed(inputs=data, output_dir=tmp_path / "state", budget=budget(),
                       config=RunConfig(prompt_version=CORE_PROMPT_VERSION), client=Client(draft))
    assert run.reason == "bootstrap_blocked"
    assert not run.final_snapshot.concepts
    assert run.chunks[0].response.candidates
    assert all("concept_policy:" in d.reason for d in run.logs[0].diagnostics)


@pytest.mark.parametrize("review_budget", [0, 5])
@pytest.mark.parametrize("version", [REVIEWED_PROMPT_VERSION, COMPACT_REVIEW_PROMPT_VERSION])
def test_reviewed_policy_cannot_self_approve_or_ignore_an_independent_veto(tmp_path, review_budget, version):
    data = inputs(tmp_path, files=1, paragraphs=1)

    def veto(payload):
        if "repair" not in payload:
            return core_response(payload)
        if version == COMPACT_REVIEW_PROMPT_VERSION:
            return {"decisions": [
                {"proposal_index": item["proposal_index"], "verdict": "veto",
                 "reason": "This role is not justified."}
                for item in payload["repair"]["failed_proposals"]]}
        return {"candidates": [], "schema_proposals": [], "pending": [{
            "reason": "Review veto: representation is not justified by this source.",
            "candidate_indices": [0],
        }]}

    root = tmp_path / "state"
    run = run_windowed(inputs=data, output_dir=root,
                       config=RunConfig(prompt_version=version),
                       budget=budget(max_repair_calls=review_budget), client=Client(veto))
    assert run.reason == "bootstrap_blocked"
    assert not run.final_snapshot.concepts
    assert run.chunks[0].response.candidates
    assert any("independent semantic admission" in d.reason for d in run.logs[0].diagnostics)
    if review_budget:
        assert "veto" in run.logs[0].pending[0].reason.lower()
    assert load_windowed_run(root).run_hash == run.run_hash


def test_compact_admission_schema_supports_strict_provider_output():
    from fabric_kg_builder.enrichment.foundry_client import _azure_strict_schema

    schema = _azure_strict_schema(SemanticAdmissionResponse.model_json_schema())
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["decisions"]


@pytest.mark.parametrize("decisions", [
    [], [{"verdict": "admit", "reason": "No index"}],
    [{"proposal_index": 1, "verdict": "admit", "reason": "Unknown"}],
    [{"proposal_index": 0, "verdict": "admit", "reason": "Duplicate"}] * 2,
])
def test_compact_admission_requires_exact_complete_index_coverage(decisions):
    with pytest.raises(ValueError):
        apply_semantic_admission({"decisions": decisions}, [{"proposal_index": 0, "proposal": {}}])


def test_compact_admission_only_selects_original_proposal_without_rewriting_evidence():
    original = {"action": "add_concept", "candidate_indices": [2], "concept": {"name": "Part"}, "reason": "original"}
    before = copy.deepcopy(original)
    projected = apply_semantic_admission(
        {"decisions": [{"proposal_index": 7, "verdict": "admit", "reason": "A valid business role."}]},
        [{"proposal_index": 7, "proposal": original}])
    assert projected["candidates"] == []
    assert projected["schema_proposals"] == [{**before, "reason": "A valid business role.", "replaces_proposal_index": 7}]
    assert original == before
