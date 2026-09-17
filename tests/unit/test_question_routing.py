from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.contracts.registry import REGISTERED_CONTRACT_VERSIONS
from fabric_kg_builder.domain.design import (
    DomainDesignDraft, DesignCapabilityError,
    evaluate_domain_design, compile_domain_design, load_domain_design,
    save_design_artifact,
)
from fabric_kg_builder.domain.models import CompetencyQuestionV2
from fabric_kg_builder.domain.question_routing import (
    QuestionRouting, QuestionRoutingContext, question_routing_context,
)
from fabric_kg_builder.domain.service import load_domain_contract, compute_contract_hash
from fabric_kg_builder.domain.review import run_deterministic_validation
from fabric_kg_builder.domain.stage import (
    preflight_l1_inputs, prepare_l1_stage, finalize_l1_stage, approve_persisted_l1_draft,
)
from tests.unit.test_domain_design import _preflight, _sketch, Client, generate_domain_design
from tests.unit.test_l1_stage import _intake, _candidates


def _routing(**changes):
    return QuestionRouting(
        **{
            "backend": "lakehouse_sql", "operation": "count",
            "rationale": "Count the required population using source observations.",
            "population": "Approved records", "grain": "One record observation",
            "filters": ["Approval state"], "source_requirements": ["Original record observations and approval state"],
            **changes,
        }
    )


def _inputs(tmp_path, explicit=None, question_text="How many approved records are in the total population?"):
    initial = _preflight(tmp_path)
    intake = _intake()
    question = {
        "id": "cq:q5", "question": question_text,
        "business_critical": True,
    }
    if explicit is not None:
        question["routing"] = explicit.model_dump(mode="json")
    intake["competency_questions"][-1] = question
    return preflight_l1_inputs(
        source_path=initial.source_path, intake_raw=intake,
        project_id=initial.base_identity.project_id, run_id=initial.run_id,
        model_version=initial.model_version, model_hash=initial.model_hash,
    )


def _mixed(proposed=True):
    raw = _sketch()
    raw["question_routes"][-1].update(
        source_key=None, target_key=None, answer_property_keys=[],
        unsupported_reason=None, rationale="SQL count remains physically unbound.",
    )
    if proposed:
        raw["question_routes"][-1]["routing"] = _routing().model_dump(mode="json")
    for item in [*raw["types"], *raw["relationships"], *raw["completeness"]]:
        item["question_ids"] = [question for question in item["question_ids"] if question != "cq:q5"]
    return raw


def _compact_mixed():
    raw = _mixed()
    raw.pop("review_concerns")
    for route in raw["question_routes"]:
        route.pop("unresolved_answer_requirements")
    return raw


@pytest.mark.parametrize("explicit", [False, True])
def test_mixed_graph_sql_survives_draft_compile_explicit_approval_and_reload(tmp_path, explicit):
    preflight = _inputs(tmp_path, _routing() if explicit else None)
    client = Client(_mixed(proposed=not explicit))
    draft = generate_domain_design(preflight, client=client)
    original_question = preflight.intake.competency_questions[-1]
    path = tmp_path / "design.json"
    save_design_artifact(path, draft)
    draft = load_domain_design(path)
    evaluation = evaluate_domain_design(draft)
    sql_result = evaluation.questions[-1]
    assert sql_result.status == "review_needed"
    assert sql_result.structural_path == []
    assert "lakehouse_sql_physical_binding_unresolved" in sql_result.execution_limitations
    assert evaluation.compiler_limitations == []
    assert evaluation.question_routing.questions[0].business_critical is True
    prepared = compile_domain_design(draft, evaluation, preflight=preflight)
    assert prepared.model_call_count == 0
    assert len(client.calls) == 1
    contract = prepared.proposal.draft_contract
    sql_question = contract.competency_questions[-1]
    assert (sql_question.id, sql_question.question, sql_question.business_critical) == (
        original_question.id, original_question.question, original_question.business_critical,
    )
    assert sql_question.routing == _routing()
    assert not contract.question_plans[-1].covered
    assert contract.completeness_question_coverage[-1].coverage_status == "unsupported"
    assert all("cq:q5" not in check.competency_question_ids for check in contract.completeness_requirements)
    assert len(contract.candidate_model.entity_types) == 2
    assert all(prop.value_type == "string" for entity in contract.candidate_model.entity_types for prop in entity.declared_properties)
    assert draft.inputs.intake == preflight.intake
    state, out = tmp_path / "l1", tmp_path / "domain.yaml"
    finalize_l1_stage(prepared, decision=None, actor=None, state_root=state, domain_path=out)
    approved = approve_persisted_l1_draft(
        actor="routing-reviewer", state_root=state, domain_path=out,
        expected_project_id=preflight.base_identity.project_id,
        expected_run_id=preflight.run_id,
        expected_proposal_hash=prepared.proposal.proposal_hash,
    )
    loaded = load_domain_contract(out)
    assert loaded.approval.status == approved.contract.approval.status == "approved"
    context = question_routing_context(loaded)
    assert context == question_routing_context(contract)
    assert context["questions"][0]["routing"]["physical_binding_state"] == "unresolved"
    assert context["questions"][0]["business_critical"] is True
    findings, coverage = run_deterministic_validation(loaded)
    assert not any(item.severity == "error" for item in findings)
    assert any(item.code == "SQL-PLAN-UNBOUND" for item in findings)
    assert next(item for item in coverage if item.question == original_question.question).supported is False


def test_explicit_routing_cannot_be_reclassified_by_model(tmp_path):
    explicit = _routing(backend="ontology_graph", operation="lookup")
    with pytest.raises(ValueError, match="conflicts with explicit intake"):
        generate_domain_design(_inputs(tmp_path, explicit), client=Client(_mixed()))


@pytest.mark.parametrize("explicit", [False, True])
def test_sql_pending_requirements_survive_compile_approval_and_public_exports(tmp_path, explicit):
    from click.testing import CliRunner
    from fabric_kg_builder.cli import cli

    note = "Review whether monthly totals exclude cancelled records before executing SQL."
    preflight = _inputs(tmp_path, _routing() if explicit else None)
    original_intake = preflight.intake.model_dump_json()
    raw = _mixed(proposed=not explicit)
    raw["question_routes"][-1]["unresolved_answer_requirements"] = [note]
    draft = generate_domain_design(preflight, client=Client(raw))
    path = tmp_path / "design.json"
    save_design_artifact(path, draft)
    draft = load_domain_design(path)
    evaluation = evaluate_domain_design(draft)
    assert note in evaluation.questions[-1].missing_fields
    assert evaluation.questions[-1].status == "review_needed"
    assert evaluation.compiler_limitations == []
    expected_context = evaluation.question_routing.model_dump(mode="json")
    assert expected_context["context_version"] == "1.1.0"
    assert expected_context["questions"][0]["pending_requirements"] == [note]
    prepared = compile_domain_design(draft, evaluation, preflight=preflight)
    contract = prepared.proposal.draft_contract
    assert prepared.candidates.question_routes[-1].pending_requirements == (note,)
    assert f"pending requirement (unresolved): {note}" in prepared.summary
    assert contract.competency_questions[-1].pending_requirements == [note]
    assert contract.competency_questions[-1].routing == _routing()
    assert not contract.question_plans[-1].covered
    state, domain = tmp_path / "l1", tmp_path / "domain.yaml"
    finalize_l1_stage(prepared, decision=None, actor=None, state_root=state, domain_path=domain)
    approve_persisted_l1_draft(
        actor="pending-requirements-reviewer", state_root=state, domain_path=domain,
        expected_project_id=preflight.base_identity.project_id,
        expected_run_id=preflight.run_id, expected_proposal_hash=prepared.proposal.proposal_hash,
    )
    approved = load_domain_contract(domain)
    assert approved.approval.status == "approved"
    assert question_routing_context(approved) == expected_context
    original_question = preflight.intake.competency_questions[-1]
    assert (
        approved.competency_questions[-1].id,
        approved.competency_questions[-1].question,
        approved.competency_questions[-1].business_critical,
    ) == (original_question.id, original_question.question, original_question.business_critical)
    for source in (path, domain):
        before = source.read_bytes()
        result = CliRunner().invoke(cli, ["domain", "question-context", "--file", str(source)])
        assert result.exit_code == 0, result.output
        exported = json.loads(result.output)
        assert exported["question_routing_context"] == expected_context
        assert exported["execution_verified"] is False
        assert source.read_bytes() == before
    assert preflight.intake.model_dump_json() == original_intake
    altered = json.loads(json.dumps(expected_context))
    altered["questions"][0]["pending_requirements"] = ["Changed scope"]
    with pytest.raises(ValueError, match="context hash mismatch"):
        QuestionRoutingContext.model_validate(altered)
    altered["context_hash"] = canonical_sha256({
        key: value for key, value in altered.items() if key != "context_hash"
    })
    with pytest.raises(ValueError, match="question authority"):
        QuestionRoutingContext.model_validate(altered).validate_against(approved)


def test_pending_requirements_extend_context_without_overriding_explicit_intake():
    from types import SimpleNamespace
    from fabric_kg_builder.domain.question_routing import routed_question_copies

    question = CompetencyQuestionV2(
        id="cq:q1", question="How many qualifying records exist?",
        business_critical=True, routing=_routing(), pending_requirements=["Original user review note"],
    )
    route = SimpleNamespace(
        question_id=question.id, routing=_routing(),
        pending_requirements=["Proposed review note"],
        unresolved_answer_requirements=["Proposed review note", "Additional design review note"],
    )
    result = routed_question_copies([question], [route])[0]
    assert result.pending_requirements == [
        "Original user review note", "Proposed review note", "Additional design review note",
    ]
    assert result.routing == question.routing == route.routing
    assert question.pending_requirements == ["Original user review note"]


def test_absent_pending_requirements_preserve_existing_routing_snapshot_bytes():
    question = CompetencyQuestionV2(
        id="cq:q1", question="How many qualifying records exist?",
        business_critical=True, routing=_routing(),
    )
    values = {
        "context_version": "1.0.0",
        "questions": [{
            "question_id": question.id, "question": question.question,
            "business_critical": True, "routing": _routing().model_dump(mode="json"),
        }],
    }
    expected = {**values, "context_hash": canonical_sha256(values)}
    actual = question_routing_context({"competency_questions": [question]})
    assert actual == expected
    assert "pending_requirements" not in question.model_dump(mode="json")
    assert QuestionRoutingContext.model_validate(expected).model_dump(mode="json") == expected


def test_intake_sql_routing_does_not_require_a_model_graph_route(tmp_path):
    preflight = _inputs(tmp_path, _routing())
    raw = _mixed(proposed=False)
    raw["question_routes"].pop()
    draft = generate_domain_design(preflight, client=Client(raw))
    evaluation = evaluate_domain_design(draft)
    assert evaluation.compiler_limitations == []
    prepared = compile_domain_design(draft, evaluation, preflight=preflight)
    assert not prepared.proposal.draft_contract.question_plans[-1].covered


def test_sql_only_design_needs_no_fabricated_ontology(tmp_path):
    preflight = _inputs(tmp_path)
    raw = _mixed()
    raw["types"], raw["properties"], raw["relationships"], raw["completeness"] = [], [], [], []
    for route in raw["question_routes"]:
        route.update(source_key=None, target_key=None, answer_property_keys=[], unsupported_reason=None)
        route["routing"] = _routing().model_dump(mode="json")
    draft = generate_domain_design(preflight, client=Client(raw))
    evaluation = evaluate_domain_design(draft)
    assert draft.sketch.types == draft.sketch.relationships == []
    assert all(question.status == "review_needed" for question in evaluation.questions)
    exported = evaluation.question_routing.model_dump(mode="json")
    assert len(exported["questions"]) == 5
    assert QuestionRoutingContext.model_validate(exported).context_hash == exported["context_hash"]
    with pytest.raises(DesignCapabilityError, match="compiler_no_ontology_needed"):
        compile_domain_design(draft, evaluation, preflight=preflight)


def test_snapshot_and_draft_routing_tamper_rejected(tmp_path):
    preflight = _inputs(tmp_path, _routing())
    context = question_routing_context(preflight.intake)
    context["questions"][0]["routing"]["population"] = "Changed scope"
    with pytest.raises(ValueError, match="routing context hash mismatch"):
        QuestionRoutingContext.model_validate(context)
    draft = generate_domain_design(preflight, client=Client(_mixed(proposed=False)))
    raw = draft.model_dump(mode="python")
    raw["inputs"]["intake"]["competency_questions"][-1]["routing"]["operation"] = "trend"
    with pytest.raises(ValueError):
        DomainDesignDraft.model_validate(raw)
    unknown = _mixed()
    unknown["question_routes"][-1]["question_id"] = "cq:unknown"
    with pytest.raises(ValueError, match="unknown|Unknown"):
        generate_domain_design(preflight, client=Client(unknown))
    rebound = question_routing_context(preflight.intake)
    rebound["questions"][0]["question_id"] = "cq:unknown"
    rebound["context_hash"] = canonical_sha256({key: value for key, value in rebound.items() if key != "context_hash"})
    with pytest.raises(ValueError, match="question authority"):
        QuestionRoutingContext.model_validate(rebound).validate_against(preflight.intake)


@pytest.mark.parametrize("operation", ["lookup", "count", "aggregate", "trend"])
def test_routing_snapshot_preserves_relevant_operation_context(operation):
    route = _routing(operation=operation, time_requirements=["Event time grouped by month"] if operation == "trend" else [])
    question = CompetencyQuestionV2(
        id="cq:q1", question="What result is required for the approved population?",
        business_critical=True, routing=route,
    )
    authority = {"competency_questions": [question]}
    context = QuestionRoutingContext.model_validate(question_routing_context(authority))
    context.validate_against(authority)
    assert context.questions[0].routing == route


@pytest.mark.parametrize("changes", [
    {"physical_binding_state": "bound"},
    {"sql": "SELECT COUNT(*) FROM invented"},
    {"table_id": "made-up-table"},
    {"backend": "automatic"},
])
def test_routing_is_not_sql_or_resolved_physical_binding(changes):
    with pytest.raises(ValueError):
        _routing(**changes)


def test_absence_preserves_question_bytes_and_historical_contract(tmp_path):
    raw = {"id": "cq:q1", "question": "Which records describe the governed subject?", "business_critical": True}
    question = CompetencyQuestionV2.model_validate(raw)
    assert question.model_dump(mode="json") == raw
    assert canonical_sha256(question) == canonical_sha256(raw)
    assert question_routing_context({"competency_questions": [question]}) is None
    assert question_routing_context({"competency_questions": ["Legacy first question", "Legacy second question"]}) is None
    path = Path(__file__).resolve().parents[2] / "examples/domains/facility-maintenance-v2.domain.yaml"
    original = path.read_bytes()
    contract = load_domain_contract(path)
    assert contract.model_dump(mode="json")["competency_questions"] == yaml.safe_load(original)["competency_questions"]
    assert question_routing_context(contract) is None
    assert path.read_bytes() == original
    assert compute_contract_hash(contract)


def test_frozen_c0_exports_do_not_embed_domain_routing():
    for model in REGISTERED_CONTRACT_VERSIONS.values():
        assert "QuestionRouting" not in json.dumps(model.model_json_schema())


def test_routing_prompt_does_not_force_quantity_ontology(tmp_path):
    client = Client(_mixed())
    draft = generate_domain_design(_inputs(tmp_path), client=client)
    prompt = client.calls[0]["system"]
    assert "not a keyword-only rule" in prompt
    assert "Lakehouse SQL" in prompt
    assert "Numeric source facts" in prompt
    assert "Quantities and\nunits need contextual ownership" not in prompt
    assert draft.inputs.intake.competency_questions[-1].routing is None
    assert draft.sketch.question_routes[-1].routing.backend == "lakehouse_sql"


@pytest.mark.parametrize("proposal_format", ["verbose", "compact"])
def test_raw_provider_routing_survives_both_existing_proposal_transports(tmp_path, proposal_format):
    preflight = _inputs(tmp_path)
    if proposal_format == "compact":
        raw = _compact_mixed()
    else:
        raw = _candidates()
        raw["question_routes"][-1].update(
            start_type_id=None, end_type_id=None, unsupported_reason=None,
            routing=_routing().model_dump(mode="json"),
        )
        check = raw["completeness_candidates"][0]["proposed_requirement"]
        check["competency_question_ids"] = check["source_question_ids"] = [f"cq:q{index}" for index in range(1, 5)]
    note = "Confirm whether withdrawn records belong in the population before SQL execution."
    raw["question_routes"][-1]["pending_requirements"] = [note]
    client = Client(raw)
    prepared = prepare_l1_stage(preflight, client=client, proposal_format=proposal_format)
    assert prepared.model_call_count == len(client.calls) == 1
    assert prepared.candidates.question_routes[-1].routing == _routing()
    assert prepared.proposal.draft_contract.competency_questions[-1].routing == _routing()
    assert prepared.candidates.question_routes[-1].pending_requirements == (note,)
    assert prepared.proposal.draft_contract.competency_questions[-1].pending_requirements == [note]


@pytest.mark.parametrize("proposal_format", ["verbose", "compact"])
def test_sql_only_provider_response_does_not_trigger_graph_repair(tmp_path, proposal_format):
    preflight = _inputs(tmp_path)
    if proposal_format == "compact":
        raw = _compact_mixed()
        raw["types"], raw["relationships"], raw["properties"], raw["completeness"] = [], [], [], []
        for route in raw["question_routes"]:
            route.update(source_key=None, target_key=None, answer_property_keys=[], unsupported_reason=None)
            route["routing"] = _routing().model_dump(mode="json")
    else:
        raw = _candidates()
        raw["semantic_type_candidates"], raw["relationship_candidates"], raw["completeness_candidates"] = [], [], []
        for route in raw["question_routes"]:
            route.update(start_type_id=None, end_type_id=None, unsupported_reason=None)
            route["routing"] = _routing().model_dump(mode="json")
    client = Client(raw)
    with pytest.raises(ValueError, match="L1_NO_ONTOLOGY_NEEDED"):
        prepare_l1_stage(preflight, client=client, proposal_format=proposal_format)
    assert len(client.calls) == 1


@pytest.mark.parametrize("question_text,value_type", [
    ("Which asset matches SKU AX-12345 in the registry?", "string"),
    ("What safety threshold applies to the 5-volt input?", "number"),
])
def test_digits_and_factual_numeric_properties_do_not_force_sql(tmp_path, question_text, value_type):
    preflight = _inputs(tmp_path, question_text=question_text)
    raw = _sketch()
    raw["properties"][-1].update(key="value", display_name="Requested fact", value_type=value_type)
    for route in raw["question_routes"]:
        route["answer_property_keys"] = ["subject.value"]
    raw["question_routes"][-1]["routing"] = _routing(
        backend="ontology_graph", operation="lookup", rationale="Factual lookup, not population analysis.",
        population=None, grain=None, filters=[], source_requirements=[],
    ).model_dump(mode="json")
    draft = generate_domain_design(preflight, client=Client(raw))
    prepared = compile_domain_design(draft, evaluate_domain_design(draft), preflight=preflight)
    contract = prepared.proposal.draft_contract
    assert contract.competency_questions[-1].question == question_text
    assert contract.competency_questions[-1].routing.backend == "ontology_graph"
    assert contract.question_plans[-1].covered
    assert next(prop for entity in contract.candidate_model.entity_types for prop in entity.declared_properties if prop.property_id == "property:subject.value").value_type == value_type


def test_mixed_intent_preserves_both_needs_and_explicit_review(tmp_path):
    preflight = _inputs(
        tmp_path, question_text="Which assets are linked to this site, and how many inspection records do they have?",
    )
    raw = _mixed()
    requirements = ["Relationship-defined site/asset population", "Inspection records for counts over that population"]
    raw["question_routes"][-1]["routing"] = _routing(
        rationale="Mixed graph scoping and analytic count need coordinated review.",
        population="Assets in the relationship-defined site scope",
        source_requirements=requirements,
    ).model_dump(mode="json")
    raw["review_concerns"] = [{
        "code": "mixed_intent_requires_review",
        "message": "Review graph population scoping and SQL analytical execution together; neither part is answered.",
        "question_ids": ["cq:q5"], "evidence_ids": [],
    }]
    draft = generate_domain_design(preflight, client=Client(raw))
    evaluation = evaluate_domain_design(draft)
    assert evaluation.questions[-1].status == "review_needed"
    assert any(item.code == "mixed_intent_requires_review" for item in evaluation.findings)
    prepared = compile_domain_design(draft, evaluation, preflight=preflight)
    context = question_routing_context(prepared.proposal.draft_contract)
    assert context["questions"][0]["routing"]["source_requirements"] == requirements
    assert "Mixed graph scoping" in context["questions"][0]["routing"]["rationale"]
    assert not prepared.proposal.draft_contract.question_plans[-1].covered


def test_explicit_sql_cannot_compile_a_graph_execution_fallback(tmp_path):
    preflight = _inputs(tmp_path, _routing())
    raw = _mixed(proposed=False)
    raw["question_routes"][-1].update(source_key="record", target_key="subject")
    draft = generate_domain_design(preflight, client=Client(raw))
    evaluation = evaluate_domain_design(draft)
    with pytest.raises(DesignCapabilityError, match="compiler_sql_route_contains_graph_path"):
        compile_domain_design(draft, evaluation, preflight=preflight)


def test_missing_sql_population_grain_and_source_notes_do_not_block_mixed_compile(tmp_path):
    incomplete = _routing(population=None, grain=None, filters=[], time_requirements=[], source_requirements=[])
    preflight = _inputs(tmp_path, incomplete)
    draft = generate_domain_design(preflight, client=Client(_mixed(proposed=False)))
    evaluation = evaluate_domain_design(draft)
    assert evaluation.questions[-1].status == "review_needed"
    assert evaluation.questions[-1].missing_fields == [
        "SQL population requirement", "SQL grain requirement", "SQL source requirements",
    ]
    assert evaluation.compiler_limitations == []
    prepared = compile_domain_design(draft, evaluation, preflight=preflight)
    context = question_routing_context(prepared.proposal.draft_contract)
    assert context["questions"][0]["routing"]["population"] is None
    assert context["questions"][0]["routing"]["source_requirements"] == []
    assert context["questions"][0]["routing"]["physical_binding_state"] == "unresolved"
