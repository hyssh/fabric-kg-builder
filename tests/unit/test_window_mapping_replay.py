"""Genuine offline discovery → design/approve → mapped replay → L3/L4 tests."""

import copy
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain.discovery import load_discovery
from tests.unit.test_domain_discovery_cli import Model, _approved, _invoke, _paths


class AlignmentModel:
    _config = SimpleNamespace(chat_deployment="offline-window-model")

    def __init__(self):
        self.calls = []

    def complete_json(self, **request):
        self.calls.append(request)
        payload = json.loads(request["user"])["input"]
        changes = []
        for row in payload["observations"]:
            if row["observed_term"] == "describes_subject" and row["source_type_id"] and row["target_type_id"]:
                target = next(c for c in payload["schema"]["concepts"] if c["kind"] == "relationship")
                changes.append({
                    "action": "add_alias", "concept_id": target["concept_id"],
                    "alias": "describes_subject", "observation_ids": [row["observation_id"]],
                    "reason": "Explicitly proposed directional record-to-subject synonym.",
                })
            if row["observed_term"] == "observed_voltage" and row["owner_type_id"]:
                target = next(c for c in payload["schema"]["concepts"]
                              if c["kind"] == "property" and c["name"] == "Observed Voltage")
                if row["owner_type_id"] in target["owner_type_ids"]:
                    changes.append({
                        "action": "add_alias", "concept_id": target["concept_id"],
                        "alias": "observed_voltage", "observation_ids": [row["observation_id"]],
                        "reason": "Owner-specific observed property alignment.",
                    })
                elif not any(c["concept_id"] == "working-property:record.voltage"
                             for c in payload["schema"]["concepts"]):
                    changes.append({
                        "action": "add_concept",
                        "concept": {
                            "concept_id": "working-property:record.voltage", "kind": "property",
                            "name": "observed_voltage", "definition": "Unapproved record-owned observation.",
                            "owner_type_ids": [row["owner_type_id"]], "value_type": "number",
                        },
                        "observation_ids": [row["observation_id"]],
                        "reason": "Keep a different owner scope in working schema, not production.",
                    })
        return {"changes": changes, "pending": []}


class ObservedModel(Model):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            result["types"][0]["display_name"] = "Service Record"
            result["types"][1]["display_name"] = "Service Subject"
            result["properties"].append({
                "owner_key": "subject", "key": "voltage", "display_name": "Observed Voltage",
                "value_type": "number", "required": False,
            })
            return result
        if "candidates" not in result:
            return result
        payload = json.loads(request["user"])["input"]
        text, offset = payload["text"], payload["offset_base"]
        for candidate in result["candidates"]:
            if candidate["candidate_kind"] == "entity":
                candidate["observed_type"] = "ServiceRecord" if candidate["local_id"] == "record-1" else "service_subject"
                candidate["label"] = "governed record" if candidate["local_id"] == "record-1" else "governed subject"
            else:
                candidate["observed_predicate"] = "describes_subject"
        subject = result["candidates"][1]
        whole = {"span_start": offset, "span_end": offset + len(text.rstrip()),
                 "quote": text.rstrip(), "model_authored_evidence_id": None}
        result["candidates"].extend([
            {"candidate_kind": "property", "owner_local_id": "subject-1",
             "observed_property": "observed_voltage", "value": 80.5, "normalized_value": 80.5,
             "temporal_key": None, "anchor": whole},
            {"candidate_kind": "property", "owner_local_id": "subject-1",
             "observed_property": "observed_voltage", "value": 999.0, "normalized_value": 999.0,
             "temporal_key": None, "anchor": copy.deepcopy(subject["anchors"][0])},
            {"candidate_kind": "property", "owner_local_id": "subject-1",
             "observed_property": "observed_voltage", "value": 12.0, "normalized_value": 12.0,
             "temporal_key": None, "anchor": {**whole, "quote": "fabricated quote"}},
            {**copy.deepcopy(result["candidates"][2]), "direction": "reverse"},
            {"candidate_kind": "property", "owner_local_id": "record-1",
             "observed_property": "observed_voltage", "value": 80.5, "normalized_value": 80.5,
             "temporal_key": None, "anchor": whole},
        ])
        return result


@pytest.fixture
def replay_case(tmp_path):
    source, intake, discovery, _, args = _paths(tmp_path, count=3)
    for path in source.glob("*.html"):
        path.write_text("<p>A governed record describes a governed subject at 80.5 volts.</p>")
    model = ObservedModel()
    _invoke([*args, "--live"], model=model)
    # Quarantine is preserved, but a received grounded subset remains design input.
    run = load_discovery(discovery)
    acceptance = None
    if not run.full_corpus_design_ready:
        acceptance = tmp_path / "coverage.json"
        _invoke([
            "domain", "accept-discovery", "--file", str(discovery), "--out", str(acceptance),
            "--actor", "reviewer", "--rationale", "Review known quarantine without clearing it.", "--accept",
        ])
    l1, domain = _approved(tmp_path, source, intake, discovery, model, discovery_acceptance=acceptance)
    state, mapping = tmp_path / "windows", tmp_path / "review.json"
    align = AlignmentModel()
    _invoke([
        "domain", "window-align", "--discovery", str(discovery), "--seed-domain", str(domain),
        "--out-state", str(state), "--window-size", "1", "--max-calls", "8", "--live",
    ], model=align)
    return source, discovery, l1, domain, state, mapping, align


def _review(case, *, accept=False, out=None):
    _, discovery, _, domain, state, mapping, _ = case
    return _invoke([
        "domain", "review-window-mapping", "--state", str(state),
        "--discovery", str(discovery), "--target-domain", str(domain),
        "--out", str(out or mapping), "--actor", "schema-reviewer",
        "--rationale", "Accept reviewed owner- and endpoint-scoped schema alignment only.",
        *(["--accept"] if accept else []),
    ])


def _enrich_args(case, l2, mapping=None):
    source, discovery, l1, domain, state, review, _ = case
    return [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(l2),
        "--discovery", str(discovery), "--replay-only",
        "--window-state", str(state), "--window-mapping", str(mapping or review),
    ]


def test_review_is_explicit_and_final_working_mapping_is_not_approval(replay_case, tmp_path):
    planned = _review(replay_case)
    assert planned["status"] == "planned" and planned["writes"] == planned["model_calls"] == 0
    assert not replay_case[5].exists()
    result = CliRunner().invoke(cli, _enrich_args(
        replay_case, tmp_path / "rejected-l2", mapping=replay_case[4] / "mapping.json",
    ))
    assert result.exit_code != 0
    assert not (tmp_path / "rejected-l2").exists()


def test_reviewed_mapping_replays_real_pipeline_without_inflating_evidence(replay_case, tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
    from fabric_kg_builder.serving.lifecycle_projection import run_l4

    original = replay_case[1].read_bytes()
    accepted = _review(replay_case, accept=True)
    assert accepted["result"]["ontology_approved"] is accepted["result"]["evidence_approved"] is False
    assert "working-property:record.voltage" in accepted["result"]["pending_concept_ids"]
    assert "working-property:record.voltage" not in accepted["result"]["concept_targets"]
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("Replay must not build a model"))
    l2 = tmp_path / "l2"
    args = _enrich_args(replay_case, l2)
    args[args.index("--window-mapping")] = "--mapping-review"
    replay = _invoke(args)
    assert replay["l2_model_calls"] == replay["targeted_model_calls"] == 0
    assert replay["original_quarantined_candidates"] >= 3
    assert replay_case[1].read_bytes() == original
    authority = json.loads((l2 / "discovery-reuse-authority.json").read_text())
    assert authority["window_mapping"]["review_hash"] == accepted["result"]["artifact_hash"]
    assert any(binding.get("binding_kind") == "reviewed_window_schema"
               for chunk in authority["chunks"] for binding in chunk["local_reference_bindings"])
    assert not any(binding.get("direction") == "reverse"
                   for chunk in authority["chunks"] for binding in chunk["local_reference_bindings"])
    assert replay["original_pending_candidates"] > 0
    l3root, l4root = tmp_path / "l3", tmp_path / "l4"
    l1, domain = replay_case[2:4]
    for command in [
        ["validate-evidence", "--state", str(l3root), "--l2-state", str(l2),
         "--l1-state", str(l1), "--domain", str(domain)],
        ["project-serving", "--state", str(l4root), "--l3-state", str(l3root),
         "--l2-state", str(l2), "--l1-state", str(l1), "--domain", str(domain)],
    ]:
        result = CliRunner().invoke(cli, command)
        assert result.exit_code == 0, result.output
    l3 = run_l3(state_root=l3root, l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    l4 = run_l4(l3, state_root=l4root)
    assert l4.rows.semantic_asserted_relationships
    assert l4.rows.semantic_asserted_properties
    assert any("PROPERTY_VALUE_UNGROUNDED" in row.reason_codes for row in l3.candidate_results)
    assert all(json.loads(row["normalized_value_json"]) == 80.5 for row in l4.rows.semantic_asserted_properties)


@pytest.mark.parametrize("field", ["discovery_hash", "domain_contract_hash", "window_run_hash"])
def test_tampered_mapping_rejected_before_calls(replay_case, tmp_path, field, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.contracts.base import canonical_sha256

    _review(replay_case, accept=True)
    review = json.loads(replay_case[5].read_text())
    review[field] = "f" * 64
    review["artifact_hash"] = canonical_sha256({k: v for k, v in review.items() if k != "artifact_hash"})
    tampered = tmp_path / f"{field}.json"
    tampered.write_text(json.dumps(review))
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("No model"))
    result = CliRunner().invoke(cli, _enrich_args(replay_case, tmp_path / "l2", mapping=tampered))
    assert result.exit_code != 0 and "WINDOW_MAPPING_BINDING_DRIFT" in result.output
    assert not (tmp_path / "l2").exists()


def test_changed_review_requires_fresh_l2_state(replay_case, tmp_path):
    _review(replay_case, accept=True)
    l2 = tmp_path / "l2"
    _invoke(_enrich_args(replay_case, l2))
    original = (l2 / "discovery-reuse-authority.json").read_bytes()
    from fabric_kg_builder.enrichment.window_mapping import review_window_mapping

    second = tmp_path / "second-review.json"
    review_window_mapping(
        state_root=replay_case[4], discovery_path=replay_case[1],
        domain_path=replay_case[3], actor="second-reviewer",
        rationale="A distinct approval decision must get a fresh authority.", output=second,
    )
    result = CliRunner().invoke(cli, _enrich_args(replay_case, l2, mapping=second))
    assert result.exit_code != 0 and "AUTHORITY_DRIFT" in result.output
    assert (l2 / "discovery-reuse-authority.json").read_bytes() == original


def test_plan_history_and_completed_resume_are_durable(replay_case, tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.domain import window_schema

    _, discovery, _, domain, state, _, model = replay_case
    before = {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()}
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("Planning builds no model"))
    args = [
        "domain", "window-align", "--discovery", str(discovery), "--seed-domain", str(domain),
        "--out-state", str(tmp_path / "planned"), "--window-size", "2",
    ]
    planned = _invoke(args)
    assert planned["writes"] == planned["model_calls"] == 0
    assert not (tmp_path / "planned").exists()
    history = _invoke(["domain", "window-history", "--state", str(state)])
    status = _invoke(["domain", "window-status", "--state", str(state)])
    run = window_schema.load_window_run(state)
    assert status["state"] == "complete"
    assert history[-1]["chunk_ids"][-1] == run.last_chunk_id
    assert sum(len(item["chunk_ids"]) for item in history) == len(load_discovery(discovery).chunks)
    for log in history:
        assert {row["schema_version"] for row in log["records"]} == {log["input_schema_version"]}
        assert {row["schema_hash"] for row in log["records"]} == {log["before_hash"]}
    assert before == {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()}
    calls = len(model.calls)
    resumed = _invoke([
        "domain", "window-align", "--discovery", str(discovery), "--seed-domain", str(domain),
        "--out-state", str(state), "--window-size", "1", "--max-calls", "0", "--live", "--resume",
    ], model=model)
    assert resumed["model_calls"] == 0 and len(model.calls) == calls
    assert resumed["result"]["artifact_hash"] == run.artifact_hash
    assert resumed["invocation"]["reused_window_count"] == len(run.logs)
    assert window_schema.load_window_run(state).artifact_hash == run.artifact_hash


def test_final_schema_is_unapproved_design_reference(replay_case, tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd

    source, discovery, _, _, state, _, _ = replay_case
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("Plan has no model"))
    args = [
        "domain", "design", "--input", str(source), "--intake", str(tmp_path / "intake.json"),
        "--discovery", str(discovery), "--window-state", str(state),
        "--out", str(tmp_path / "new-design.json"),
    ]
    if (tmp_path / "coverage.json").exists():
        args += ["--discovery-acceptance", str(tmp_path / "coverage.json")]
    result = _invoke(args)
    reference = result["result"]["window_schema_reference"]
    assert reference["authority"] == "working_reference_only"
    assert reference["ontology_approved"] is reference["evidence_approved"] is False
    assert not (tmp_path / "new-design.json").exists()


def test_partial_public_run_resumes_to_last_chunk_without_repeating_calls(replay_case, tmp_path):
    _, discovery, _, domain, _, _, _ = replay_case
    state = tmp_path / "bounded-windows"
    model = AlignmentModel()
    args = [
        "domain", "window-align", "--discovery", str(discovery), "--seed-domain", str(domain),
        "--out-state", str(state), "--window-size", "1", "--live",
    ]
    partial = _invoke([*args, "--max-calls", "1"], model=model)
    assert partial["status"] == "partial" and len(model.calls) == 1
    completed = _invoke([*args, "--max-calls", "8", "--resume"], model=model)
    assert completed["status"] == "complete" and len(model.calls) == 3
    history = _invoke(["domain", "window-history", "--state", str(state)])
    chunks = [chunk for log in history for chunk in log["chunk_ids"]]
    assert len(chunks) == len(set(chunks)) == len(load_discovery(discovery).chunks)
    assert completed["result"]["last_chunk_id"] == chunks[-1]


def test_explicit_deterministic_mode_never_builds_model_and_resume_preserves_mode(replay_case, tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.domain.window_schema import load_final_mapping

    _, discovery, _, domain, _, _, _ = replay_case
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("Deterministic mode has no model"))
    state = tmp_path / "deterministic"
    args = [
        "domain", "window-align", "--discovery", str(discovery), "--seed-domain", str(domain),
        "--out-state", str(state), "--window-size", "2", "--live",
    ]
    run = _invoke([*args, "--proposal-mode", "deterministic"])
    assert run["status"] == "complete" and run["model_calls"] == 0
    mapping = load_final_mapping(state)
    assert any(row.observed_term == "describes_subject" and row.status == "pending" for row in mapping.records)
    assert any(row.kind == "entity" and row.status == "mapped" for row in mapping.records)
    resumed = _invoke([*args, "--resume", "--max-calls", "0"])
    assert resumed["status"] == "complete" and resumed["model_calls"] == 0
    changed = CliRunner().invoke(cli, [*args, "--resume", "--max-calls", "0", "--proposal-mode", "model"])
    assert changed.exit_code != 0


def _identity_mapping_case(*, subtype=False, raw_key="equipment_id"):
    from fabric_kg_builder.contracts.base import canonical_sha256
    from fabric_kg_builder.domain.hierarchy import build_type_hierarchy_closure
    from fabric_kg_builder.domain.models import DomainContractV2
    from fabric_kg_builder.domain.window_schema import DesignReference, WorkingConcept, seed_snapshot
    from fabric_kg_builder.enrichment.schema2_extraction import RawCandidateResponse
    from fabric_kg_builder.enrichment.window_mapping import ReplayMapping
    from tests.unit.test_schema2_extraction import _domain, _response

    base = _domain()
    root_id = "semantic-type:facility-maintenance.equipment"
    property_id = "property:asset.serial" if subtype else "property:equipment-id"
    approved_key = property_id if subtype else "equipment_id"
    entities = list(base.candidate_model.entity_types)
    root = next(entity for entity in entities if entity.type_id == root_id)
    raw = root.model_dump(mode="json")
    raw["identity_key_policy"]["business_key_fields"] = [approved_key]
    raw["declared_properties"] = [{
        "property_id": property_id, "display_name": "Serial" if subtype else "Equipment ID",
        "value_type": "string", "required": False,
    }]
    root = type(root).model_validate(raw)
    entities = [root if entity.type_id == root_id else entity for entity in entities]
    selected = root
    concepts = [
        WorkingConcept(concept_id=root_id, kind="entity", name=root.display_name, definition=root.description),
        WorkingConcept(
            concept_id=property_id, kind="property", name=raw["declared_properties"][0]["display_name"],
            definition="Reviewed identity field", owner_type_ids=[root_id],
            endpoint_policy="allow_subtypes", aliases=["serial_no"] if subtype else ["equipment_id", "device_no"],
        ),
    ]
    if subtype:
        child = root.model_dump(mode="json")
        child.update(type_id="semantic-type:facility-maintenance.pump", semantic_key="pump",
                     display_name="Pump", parent_type_id=root_id, identity_key_policy=None,
                     declared_properties=[], aliases=[], generalization_basis={
                         "competency_question_ids": [base.competency_questions[0].id],
                         "evidence_span_ids": [], "governance_rationale": "Reviewed equipment specialization.",
                     })
        selected = type(root).model_validate(child)
        entities.append(selected)
        concepts.append(WorkingConcept(
            concept_id=selected.type_id, kind="entity", name="Pump", definition="Asset subtype",
            parent_type_id=root_id,
        ))
    model = base.candidate_model.model_copy(update={"entity_types": entities})
    contract = base.model_copy(update={
        "candidate_model": model,
        "hierarchy_closure": build_type_hierarchy_closure(entities, model.relationship_types),
        "identity_policy_hash": canonical_sha256({
            entity.type_id: entity.identity_key_policy.model_dump(mode="json")
            for entity in entities if entity.parent_type_id is None
        }),
    })
    contract = DomainContractV2.model_validate(contract.model_dump(mode="json"))
    candidate = {**_response()[1], "observed_type": selected.display_name,
                 "identity_key": {raw_key: "E-17"}, "stable_source_identity": None}
    candidate = RawCandidateResponse.model_validate({"candidates": [candidate]}).model_dump(mode="json")["candidates"][0]
    digest = canonical_sha256(candidate)
    record = SimpleNamespace(
        observation_id="observation:identity", chunk_id="chunk:identity",
        status="mapped", concept_id=selected.type_id, verified_candidate_hash=digest,
        raw_candidate_hash=digest, kind="entity", observed_term=selected.display_name,
        source_type_id=None, target_type_id=None, owner_type_id=None, direction=None,
    )
    snapshot = seed_snapshot(DesignReference(concepts=concepts))
    mapping = ReplayMapping(
        SimpleNamespace(concept_targets={c.concept_id: c.concept_id for c in concepts}, artifact_hash="a" * 64),
        SimpleNamespace(records=[record]), snapshot,
    )
    return contract, {"candidates": [candidate]}, mapping, approved_key


@pytest.mark.parametrize("subtype,raw_key", [
    (False, "equipment_id"), (False, "device_no"), (True, "serial_no"),
])
def test_reviewed_identity_keys_use_actual_inherited_policy_fields(subtype, raw_key):
    from fabric_kg_builder.enrichment.discovery_reuse import map_discovery_candidates
    from fabric_kg_builder.enrichment.schema2_extraction import compile_closed_vocabulary

    contract, raw, mapping, approved_key = _identity_mapping_case(subtype=subtype, raw_key=raw_key)
    original = copy.deepcopy(raw)
    result = map_discovery_candidates(
        raw, chunk_id="chunk:identity", contract=contract,
        vocabulary=compile_closed_vocabulary(contract), window_mapping=mapping,
    )
    assert raw == original
    assert result.response["candidates"][0]["identity_key"] == {approved_key: "E-17"}
    assert "identity_fields_missing_or_ambiguous" not in result.pending_reasons
    if raw_key == approved_key:
        baseline = map_discovery_candidates(
            raw, chunk_id="chunk:identity", contract=contract,
            vocabulary=compile_closed_vocabulary(contract),
        )
        assert result.response["candidates"][0]["identity_key"] == baseline.response["candidates"][0]["identity_key"]
        assert result.pending_reasons == baseline.pending_reasons


def test_identity_alias_cannot_bypass_exact_owner_scope():
    from fabric_kg_builder.domain.window_schema import DesignReference, seed_snapshot
    from fabric_kg_builder.enrichment.window_mapping import align_verified_candidates

    contract, raw, mapping, _ = _identity_mapping_case(subtype=True, raw_key="serial_no")
    mapping.snapshot = seed_snapshot(DesignReference(concepts=[
        concept.model_copy(update={"endpoint_policy": "exact"}) if concept.kind == "property" else concept
        for concept in mapping.snapshot.concepts
    ]))
    aligned, _ = align_verified_candidates(raw["candidates"], mapping, chunk_id="chunk:identity", contract=contract)
    assert aligned[0]["identity_key"] == {"serial_no": "E-17"}
