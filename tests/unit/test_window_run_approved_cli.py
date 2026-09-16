"""Authentic integrated observations → public approved CLI → unchanged L3/L4."""

import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain.design import load_domain_design
from fabric_kg_builder.domain.service import load_domain_contract
from tests.unit.test_domain_discovery_cli import _invoke
from tests.unit.test_window_mapping_replay import ObservedModel


class IntegratedModel(ObservedModel):
    def complete_json(self, **request):
        if "decisions" in request["json_schema"]["properties"]:
            payload = json.loads(request["user"])["input"]
            return {"decisions": [
                {"proposal_index": item["proposal_index"], "verdict": "admit",
                 "reason": "Independently reviewed reusable business role."}
                for item in payload["repair"]["failed_proposals"]
            ]}
        result = super().complete_json(**request)
        if "types" in result:
            result["question_routes"][-1]["unresolved_answer_requirements"] = [
                "Confirm cancellation rules before executing SQL."
            ]
            return result
        payload = json.loads(request["user"])["input"]
        for candidate in result["candidates"]:
            if candidate["candidate_kind"] == "entity":
                candidate["observed_type"] = "Service Record" if candidate["local_id"] == "record-1" else "Service Subject"
            elif candidate["candidate_kind"] == "relationship":
                candidate["observed_predicate"] = "Describes"
            else:
                candidate["observed_property"] = "Observed Voltage"
        present = {item["concept_id"] for item in payload["schema"]["concepts"]}
        concepts = [
            (0, {"concept_id": "working:record", "kind": "entity", "name": "Service Record",
                 "definition": "A source service record.", "identity_policy": {"mode": "unresolved"}}),
            (1, {"concept_id": "working:subject", "kind": "entity", "name": "Service Subject",
                 "definition": "A common service subject.", "identity_policy": {"mode": "unresolved"}}),
            (2, {"concept_id": "working:describes", "kind": "relationship", "name": "Describes",
                 "definition": "Record describes a subject.",
                 "source_type_ids": ["working:record"], "target_type_ids": ["working:subject"],
                 "identity_policy": {"context_policy": "source record"}}),
            (3, {"concept_id": "working:voltage", "kind": "property", "name": "Observed Voltage",
                 "definition": "Subject voltage.",
                 "owner_type_ids": ["working:subject"], "value_type": "number"}),
        ]
        response = {
            "candidates": result["candidates"],
            "schema_proposals": [
                {"action": "add_concept", "concept": concept, "candidate_indices": [index],
                 "reason": "Retain a directly observed concept with its exact owner/endpoints.",
                 **({"abstraction": {
                     "level": "reusable_type",
                     "rationale": "Reusable service role or attribute independent of instance labels.",
                     "reuse_assessment": "No existing concept has this business role and scope.",
                     **({
                         "representation": {"entity": "entity_type", "relationship": "relationship_type",
                                            "property": "property"}[concept["kind"]],
                         "independent_identity_rationale": "Independently identifiable service business role.",
                     } if "CORE BUSINESS MODEL ADMISSION" in request["system"] else {}),
                 }} if "CONCEPT-FIRST ONTOLOGY POLICY" in request["system"] else {})}
                for index, concept in concepts if concept["concept_id"] not in present
            ],
            "pending": [], "working_context": {"common_concepts": ["working:subject"]},
        }
        if "repair" in payload:
            response["candidates"] = []
            for proposal in response["schema_proposals"]:
                proposal["replaces_proposal_index"] = next(
                    item["proposal_index"] for item in payload["repair"]["failed_proposals"]
                    if item["proposal"]["concept"]["concept_id"] == proposal["concept"]["concept_id"])
                proposal["reason"] = "Independent review confirms the reusable business role and scope."
        return response


@pytest.fixture(params=["concepts", "reviewed-concepts"])
def integrated_case(tmp_path, request):
    from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
    from fabric_kg_builder.domain.discovery import prepare_discovery_corpus
    from fabric_kg_builder.domain.stage import preflight_l1_inputs
    from fabric_kg_builder.sources.preparation import indexed_corpus_reader
    from tests.unit.test_domain_discovery_cli import _paths

    source, intake, _, _, _ = _paths(tmp_path, count=1)
    for path in source.glob("*.html"):
        path.write_text("<p>A governed record describes a governed subject at 80.5 volts.</p>")
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=json.loads(intake.read_text()),
        project_id="project:records", run_id="run:offline-prepared",
        model_version="offline", model_hash=canonical_sha256({"offline": True}),
    )
    reader = indexed_corpus_reader(preflight.corpus, source, project_id="project:records")
    prepared = prepare_discovery_corpus(preflight, reader=reader)
    prepared_path = tmp_path / "prepared.json"
    prepared_path.write_text(canonical_json(prepared))
    windows = tmp_path / "windows"
    model = IntegratedModel()
    result = _invoke([
        "domain", "window-run", "--prepared", str(prepared_path), "--intake", str(intake),
        "--discovery-mode", "chunked", "--schema-policy", request.param,
        "--out-state", str(windows), "--window-size", "1", "--concurrency", "1",
        "--max-tokens", "1000000", "--max-calls", "8", "--max-repair-calls", "8", "--live",
    ], model=model)
    assert result["status"] == "complete"
    assert result["result"]["prompt_version"] == (
        "raw-working-window/1.3.0" if request.param == "concepts" else "raw-working-window/1.5.0")
    l1, domain, draft = _approve_integrated(tmp_path, source, intake, windows, model)
    return tmp_path, source, intake, windows, model, l1, domain, draft


def _approve_integrated(root, source, intake, windows, model, *, acceptance=None):
    draft, evaluation = root / "design.json", root / "evaluation.json"
    _invoke([
        "domain", "design", "--input", str(source), "--intake", str(intake),
        "--window-run", str(windows), "--out", str(draft), "--live",
        *(["--window-run-acceptance", str(acceptance)] if acceptance is not None else []),
    ], model=model)
    evaluated = _invoke(["domain", "evaluate-design", "--file", str(draft), "--out", str(evaluation)])
    l1, domain = root / "l1", root / "domain.yaml"
    compiled = _invoke([
        "domain", "compile-design", "--input", str(source), "--file", str(draft),
        "--evaluation", str(evaluation), "--accept-evaluation-hash", evaluated["result"]["evaluation_hash"],
        "--out-state", str(l1), "--out-domain", str(domain),
    ])
    assert load_domain_contract(domain).approval.status != "approved"
    approved = CliRunner().invoke(cli, [
        "domain", "approve", "--file", str(domain), "--state-dir", str(l1),
        "--approved-by", "offline-reviewer", "--project-id", compiled["result"]["project_id"],
        "--run-id", compiled["result"]["run_id"], "--proposal-hash", compiled["result"]["proposal_hash"],
    ])
    assert approved.exit_code == 0, (approved.output, approved.exception)
    return l1, domain, draft


def _review(windows, domain, review, *, accept=True):
    result = CliRunner().invoke(cli, [
        "domain", "review-window-run-mapping", "--window-run", str(windows),
        "--target-domain", str(domain), "--out", str(review),
        "--actor", "offline-mapping-reviewer", "--rationale", "Review exact labels, approved owners and endpoints.",
        *(["--accept"] if accept else []),
    ])
    assert result.exit_code == 0, (result.output, result.exception)
    return json.loads(result.output)


def _replay_args(source, windows, domain, l1, l2, review):
    return [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(l2),
        "--window-run", str(windows), "--mapping-review", str(review), "--replay-only",
    ]


def _check_pipeline(root, source, windows, domain, l1, draft, review, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
    from fabric_kg_builder.serving.lifecycle_projection import run_l4
    from fabric_kg_builder.domain.question_routing import question_routing_context

    design = load_domain_design(draft)
    contract = load_domain_contract(domain)
    assert design.discovery is None and design.window_run is not None
    assert contract.discovery_run_hash is None
    assert contract.window_run_binding.window_run_hash == design.window_run.artifact_hash
    assert contract.window_run_binding.context_hash == design.window_run.context.artifact_hash
    assert "coverage_acceptance_hash" not in contract.window_run_binding.model_dump(mode="json")
    assert "window_run_acceptance" not in contract.model_dump(mode="json")
    assert len(contract.competency_questions) == len(design.window_run.context.intake_raw["competency_questions"])
    assert contract.competency_questions[-1].routing.backend == "lakehouse_sql"
    assert not contract.question_plans[-1].covered
    assert "Confirm cancellation rules before executing SQL." in question_routing_context(contract)["questions"][0]["pending_requirements"]
    assert design.sketch.types[1].classification == "common"
    planned = _review(windows, domain, review, accept=False)
    assert planned["writes"] == 0 and not review.exists()
    accepted = _review(windows, domain, review)
    targets = accepted["result"]["concept_targets"]
    assert len(targets) >= 4 and any(key != value for key, value in targets.items())
    assert accepted["result"]["ontology_approved"] is False
    before = {path: path.read_bytes() for path in windows.rglob("*.json")}
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("No model during replay"))
    l2 = root / "l2"
    result = _invoke(_replay_args(source, windows, domain, l1, l2, review))
    assert result["operation"] == "enrich.window-run-reuse"
    assert result["l2_model_calls"] == result["targeted_model_calls"] == 0
    assert result["original_quarantined_candidates"] > 0
    assert result["original_candidate_ledger_complete"]
    assert all(path.read_bytes() == content for path, content in before.items())
    authority = json.loads((l2 / "window-run-reuse-authority.json").read_text())
    assert authority["artifact_kind"] == "window_run.approved_reuse"
    assert "discovery_hash" not in authority
    assert authority["window_mapping"]["review_hash"] == accepted["result"]["artifact_hash"]
    l3, l4 = root / "l3", root / "l4"
    for args in [
        ["validate-evidence", "--state", str(l3), "--l2-state", str(l2),
         "--l1-state", str(l1), "--domain", str(domain)],
        ["project-serving", "--state", str(l4), "--l3-state", str(l3),
         "--l2-state", str(l2), "--l1-state", str(l1), "--domain", str(domain)],
    ]:
        response = CliRunner().invoke(cli, args)
        assert response.exit_code == 0, (response.output, response.exception)
    validated = run_l3(state_root=l3, l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    served = run_l4(validated, state_root=l4)
    assert served.rows.semantic_asserted_relationships
    assert served.rows.semantic_asserted_properties
    assert any("PROPERTY_VALUE_UNGROUNDED" in row.reason_codes for row in validated.candidate_results)
    assert all(json.loads(row["normalized_value_json"]) == 80.5 for row in served.rows.semantic_asserted_properties)


def test_integrated_run_approved_public_cli_to_l3_l4(integrated_case, monkeypatch):
    root, source, intake, windows, model, l1, domain, draft = integrated_case
    design_request = next(request for request in model.calls if "types" in request["json_schema"]["properties"])
    assert "common_concepts" in design_request["user"] and "schema_review" in design_request["user"]
    assert all((question["question"] if isinstance(question, dict) else question) in design_request["user"]
               for question in json.loads(intake.read_text())["competency_questions"])
    _check_pipeline(root, source, windows, domain, l1, draft, root / "review.json", monkeypatch)


def test_integrated_context_and_review_drift_refused(integrated_case, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.contracts.base import canonical_sha256

    root, source, intake, windows, _, l1, domain, _ = integrated_case
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("No model on drift"))
    changed = root / "different-intake.json"
    raw = json.loads(intake.read_text())
    raw["business_goal"] += " changed"
    changed.write_text(json.dumps(raw))
    result = CliRunner().invoke(cli, [
        "domain", "design", "--input", str(source), "--intake", str(changed),
        "--window-run", str(windows), "--out", str(root / "different-design.json"), "--live",
    ])
    assert result.exit_code != 0 and "WINDOW_RUN_CONTEXT_DRIFT" in result.output
    review = root / "review.json"
    _review(windows, domain, review)
    payload = json.loads(review.read_text())
    payload["binding"]["window_run_hash"] = "f" * 64
    payload["artifact_hash"] = canonical_sha256({k: v for k, v in payload.items() if k != "artifact_hash"})
    tampered = root / "tampered-review.json"
    tampered.write_text(json.dumps(payload))
    result = CliRunner().invoke(cli, _replay_args(source, windows, domain, l1, root / "l2", tampered))
    assert result.exit_code != 0 and "WINDOW_RUN_MAPPING_BINDING_DRIFT" in result.output
    assert not (root / "l2").exists()


def test_integrated_replay_requires_explicit_mapping_and_fresh_l2(integrated_case):
    root, source, _, windows, _, l1, domain, _ = integrated_case
    review = root / "review.json"
    _review(windows, domain, review)
    args = _replay_args(source, windows, domain, l1, root / "l2", review)
    pos = args.index("--mapping-review")
    missing = CliRunner().invoke(cli, args[:pos] + args[pos + 2:])
    assert missing.exit_code != 0 and "requires --mapping-review" in missing.output
    (root / "l2").mkdir()
    (root / "l2" / "foreign-state.json").write_text("{}")
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0 and "REQUIRES_FRESH_L2_STATE" in result.output
    assert not (root / "l2" / "window-run-reuse-authority.json").exists()


def test_partial_window_run_cannot_enter_design_even_with_full_prepared_sources(integrated_case, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.domain.window_run import load_window_inputs, run_windowed

    root, source, intake, _, _, _, _, _ = integrated_case
    partial = root / "partial-windows"
    result = run_windowed(
        inputs=load_window_inputs(prepared_path=root / "prepared.json", intake_path=intake),
        output_dir=partial,
    )
    assert result.state == "partial" and result.cursor == 0
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("No model on incomplete coverage"))
    output = root / "partial-design.json"
    response = CliRunner().invoke(cli, [
        "domain", "design", "--input", str(source), "--intake", str(intake),
        "--window-run", str(partial), "--out", str(output), "--live",
    ])
    assert response.exit_code != 0 and "WINDOW_RUN_INCOMPLETE" in response.output
    assert not output.exists()


def test_changed_window_source_refused_before_replay(integrated_case, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd

    root, source, _, windows, _, l1, domain, _ = integrated_case
    review = root / "review.json"
    _review(windows, domain, review)
    source_file = next(source.glob("*.html"))
    source_file.write_text("<p>Different source bytes.</p>")
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("No model on source drift"))
    response = CliRunner().invoke(cli, _replay_args(source, windows, domain, l1, root / "l2", review))
    assert response.exit_code != 0
    assert not (root / "l2").exists()


def test_approved_window_domain_cannot_implicitly_reextract(integrated_case):
    root, source, _, _, _, l1, domain, _ = integrated_case
    response = CliRunner().invoke(cli, [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(root / "l2"),
    ])
    assert response.exit_code != 0 and "Implicit re-extraction is forbidden" in response.output
    assert not (root / "l2").exists()


def test_mapping_preview_cannot_authorize_replay(integrated_case, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd

    root, source, _, windows, _, l1, domain, _ = integrated_case
    review = root / "review.json"
    preview = _review(windows, domain, review, accept=False)
    assert preview["status"] == "planned" and preview["writes"] == 0
    assert preview["result"]["accepted"] is False
    assert preview["result"]["authority"] == "review_required"
    assert "artifact_hash" not in preview["result"]
    assert not review.exists()
    redirected = root / "redirected-preview.json"
    redirected.write_text(json.dumps(preview["result"]))
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("Preview cannot trigger extraction"))
    response = CliRunner().invoke(cli, _replay_args(source, windows, domain, l1, root / "l2", redirected))
    assert response.exit_code != 0 and "validation" in response.output.lower()
    assert not (root / "l2").exists()


def test_mapping_accept_conflicts_with_global_dry_run(integrated_case):
    root, _, _, windows, _, _, domain, _ = integrated_case
    review = root / "review.json"
    response = CliRunner().invoke(cli, [
        "--dry-run", "domain", "review-window-run-mapping", "--window-run", str(windows),
        "--target-domain", str(domain), "--out", str(review),
        "--actor", "reviewer", "--rationale", "Review exact approved scopes.", "--accept",
    ])
    assert response.exit_code == 2 and "--accept conflicts with --dry-run" in response.output
    assert not review.exists()
