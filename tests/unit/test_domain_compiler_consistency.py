import pytest

from fabric_kg_builder.domain.compact import expand_compact_design
from fabric_kg_builder.domain.design import (
    DesignCapabilityError, compile_domain_design, evaluate_domain_design,
)
from fabric_kg_builder.domain.stage import L1ProposalSchemaRepairError
from tests.unit.test_domain_design import Client, _preflight, _sketch, generate_domain_design
from tests.unit.test_question_routing import _inputs, _mixed, _routing


@pytest.mark.parametrize("kind", ["collection", "required_role"])
@pytest.mark.parametrize("explicit", [False, True])
def test_shared_graph_sql_requirement_does_not_poison_graph_coverage(tmp_path, kind, explicit):
    preflight = _inputs(tmp_path, _routing() if explicit else None)
    raw = _mixed(proposed=not explicit)
    raw["relationships"][0]["question_ids"].append("cq:q5")
    raw["completeness"][0]["question_ids"].append("cq:q5")
    raw["completeness"][0]["kind"] = kind
    draft = generate_domain_design(preflight, client=Client(raw))
    frozen = draft.model_dump_json()
    evaluation = evaluate_domain_design(draft)
    assert not evaluation.compiler_limitations
    prepared = compile_domain_design(draft, evaluation, preflight=preflight)
    contract = prepared.proposal.draft_contract
    requirement = contract.completeness_requirements[0]
    assert requirement.competency_question_ids == raw["completeness"][0]["question_ids"]
    assert requirement.coverage_status == "covered"
    coverage = {item.question_id: item.coverage_status for item in contract.completeness_question_coverage}
    assert all(coverage[f"cq:q{index}"] == "covered" for index in range(1, 5))
    assert coverage["cq:q5"] == "unsupported"
    assert not next(plan for plan in contract.question_plans if plan.question_id == "cq:q5").covered
    assert contract.competency_questions[-1].routing == _routing()
    assert draft.model_dump_json() == frozen
    assert prepared.model_call_count == 0


def test_sql_only_requirement_remains_unsupported(tmp_path):
    preflight = _inputs(tmp_path, _routing())
    raw = _mixed(proposed=False)
    raw["relationships"][0]["question_ids"].append("cq:q5")
    raw["completeness"].append({
        **raw["completeness"][0], "key": "sql_only", "question_ids": ["cq:q5"],
    })
    draft = generate_domain_design(preflight, client=Client(raw))
    candidates = expand_compact_design(
        draft.sketch, intake=preflight.intake,
        known_evidence_ids={item.evidence_span_id for item in draft.samples.evidence_spans},
        derive_route_metadata=False,
    )
    requirement = next(item.proposed_requirement for item in candidates.completeness_candidates if item.proposed_requirement.requirement_id.endswith(":sql_only"))
    assert requirement.coverage_status == "unsupported"


def test_compilation_surfaces_bounded_structured_validation_detail(tmp_path, monkeypatch):
    preflight = _preflight(tmp_path)
    draft = generate_domain_design(preflight, client=Client(_sketch()))
    failure = ("proposal.draft_contract.root", "critical_question_coverage_incomplete")

    def reject(*args, **kwargs):
        raise L1ProposalSchemaRepairError(
            attempt_count=1, validation_failures=(failure,),
            validation_details={failure: "business-critical coverage missing for cq:q1. " + "x" * 300 + "FULL_SOURCE_DUMP"},
        )

    monkeypatch.setattr("fabric_kg_builder.domain.design.prepare_l1_stage", reject)
    with pytest.raises(DesignCapabilityError) as caught:
        compile_domain_design(draft, evaluate_domain_design(draft), preflight=preflight)
    finding = caught.value.findings[0]
    assert finding.code == failure[1]
    assert "cq:q1" in finding.message
    assert len(finding.message) <= 402
    assert "FULL_SOURCE_DUMP" not in str(caught.value)
