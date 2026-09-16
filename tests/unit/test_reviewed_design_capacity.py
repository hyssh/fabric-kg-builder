from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.domain.compact import (
    CompactDesignError, CompactDesignSketch, ReviewedCompactDesignSketch,
    compact_design_schema, expand_compact_design,
)
from fabric_kg_builder.domain.compiler_capacity import REVIEWED_DESIGN_64
from fabric_kg_builder.domain.design import (
    DesignCapabilityError, DomainDesignEvaluation, compile_domain_design,
    evaluate_domain_design, load_domain_design, save_design_artifact,
)
from fabric_kg_builder.domain.models import DomainContractV2, ReasoningPolicyV2
from fabric_kg_builder.domain.selection import ProposalSelectionError, select_relationship_vocabulary
from fabric_kg_builder.domain.stage import (
    L1StageError, _selector_hash, finalize_l1_stage, load_prepared_l1_stage, prepare_l1_stage,
)
from tests.unit.test_domain_design import (
    Client, QUESTIONS, _preflight, _sketch, _type, generate_domain_design,
)


def _revision_nine_sketch():
    # Structural transcription of normalized revision 9, artifact hash
    # 41fb40ebba1357767ff2670ed22a7324883b2adee0c39923467831efa2b78e95.
    entities = [
        "procedure", "procedure_step", "tool", "consumable", "advisory",
        "device_model", "device_variant", "applicability_criterion",
        "device_component", "service_part", "fastener", "repair_item_requirement",
    ]
    scoped = ["procedure", "procedure_step", "advisory", "service_part", "repair_item_requirement"]
    concepts = [
        ("has_step", ["procedure"], ["procedure_step"], True),
        ("references_procedure", ["procedure_step"], ["procedure"], True),
        ("has_advisory", ["procedure", "procedure_step"], ["advisory"], True),
        ("is_variant_of", ["device_variant"], ["device_model"], False),
        ("has_criterion", ["device_variant"], ["applicability_criterion"], False),
        ("applies_to_product_scope", scoped, ["device_model", "device_variant"], False),
        ("targets_component", ["procedure"], ["device_component"], True),
        ("replaces_component", ["service_part"], ["device_component"], True),
        ("includes_item", ["service_part"], ["device_component", "service_part", "consumable"], True),
        ("has_requirement", ["procedure"], ["repair_item_requirement"], False),
        ("specifies_item", ["repair_item_requirement"], ["service_part", "tool", "consumable"], True),
        ("has_applicability_criterion", scoped, ["applicability_criterion"], False),
        ("has_substep", ["procedure_step"], ["procedure_step"], False),
    ]
    properties = [
        ("sequence_number", ["procedure_step"], "integer"),
        ("instruction_text", ["procedure_step"], "string"),
        ("phase", ["procedure_step"], "string"),
        ("advisory_kind", ["advisory"], "string"),
        ("quantity", ["repair_item_requirement"], "integer"),
        ("quantity_unit", ["repair_item_requirement"], "string"),
        ("requirement_category", ["repair_item_requirement"], "string"),
        ("part_number", ["service_part", "fastener"], "string"),
        ("criterion_text", ["applicability_criterion"], "string"),
        ("advisory_text", ["advisory"], "string"),
        ("ordinal_label", ["procedure_step"], "string"),
        ("quantity_expression", ["repair_item_requirement"], "string"),
        ("alternate_part_number", ["service_part", "fastener"], "string"),
        ("availability_category", ["service_part", "fastener"], "string"),
    ]

    def endpoints(keys, subtypes):
        return sorted(set(keys) | ({"fastener"} if subtypes and "service_part" in keys else set()))

    pairs = [
        (name, source, target)
        for name, sources, targets, subtypes in concepts
        for source in endpoints(sources, subtypes)
        for target in endpoints(targets, subtypes)
    ]
    assert (len(entities), len(concepts), len(properties), len(pairs)) == (12, 13, 14, 38)
    raw = _sketch()
    raw["types"] = [_type(key, parent="service_part" if key == "fastener" else None) for key in entities]
    raw["properties"] = [
        {"owner_key": owner, "key": key, "display_name": key, "value_type": value_type, "required": False}
        for key, owners, value_type in properties for owner in owners
    ]
    raw["relationships"] = [
        {
            **raw["relationships"][0], "key": f"r_{index:02d}",
            "display_name": name, "description": f"{source} {name} {target}.",
            "source_key": source, "target_key": target,
            "rationale": f"Retain the reviewed {name} concept's exact endpoint pair {source}/{target}.",
        }
        for index, (name, source, target) in enumerate(pairs)
    ]
    raw["question_routes"] = [
        {
            **route, "source_key": "procedure", "target_key": "procedure_step",
            "answer_property_keys": ["procedure_step.instruction_text"],
        }
        for route in raw["question_routes"]
    ]
    raw["completeness"][0]["relationship_key"] = "r_00"
    return raw, pairs


def test_revision_nine_38_explicit_pairs_compile_losslessly_and_persist(tmp_path):
    preflight = _preflight(tmp_path)
    raw, pairs = _revision_nine_sketch()
    draft = generate_domain_design(preflight, client=Client(raw))
    original = draft.model_dump(mode="json")
    legacy = evaluate_domain_design(draft)
    assert "compiler_relationship_limit" in {item.code for item in legacy.compiler_limitations}
    assert "compiler_capability" not in legacy.model_dump(mode="json")
    with pytest.raises(DesignCapabilityError):
        compile_domain_design(draft, legacy, preflight=preflight)

    reviewed = evaluate_domain_design(draft, compiler_capability=REVIEWED_DESIGN_64)
    assert not reviewed.compiler_limitations
    assert reviewed.evaluation_hash != legacy.evaluation_hash
    prepared = compile_domain_design(draft, reviewed, preflight=preflight)
    contract = prepared.proposal.draft_contract
    assert len(contract.candidate_model.entity_types) == 12
    actual = {
        (item.display_name, item.source_type_ids[0].split(":")[1], item.target_type_ids[0].split(":")[1])
        for item in contract.candidate_model.relationship_types
    }
    assert actual == set(pairs)
    assert sum(len(item.declared_properties) for item in contract.candidate_model.entity_types) == len(raw["properties"])
    assert (contract.reasoning_policy.relationship_type_count, contract.reasoning_policy.max_relationship_types) == (38, 64)
    assert contract.reasoning_policy.compiler_capability == REVIEWED_DESIGN_64
    assert len(contract.reasoning_policy.retained_type_rationales) == 38
    assert prepared.design_context.selector_hash == _selector_hash(REVIEWED_DESIGN_64)
    assert _selector_hash() != _selector_hash(REVIEWED_DESIGN_64)
    assert prepared.model_call_count == 0
    assert contract.approval.status == "draft"
    assert draft.model_dump(mode="json") == original
    path = tmp_path / "unchanged-design.json"
    save_design_artifact(path, draft)
    assert load_domain_design(path) == draft
    state = tmp_path / "state"
    finalize_l1_stage(prepared, decision=None, actor=None, state_root=state, domain_path=tmp_path / "domain.yaml")
    assert load_prepared_l1_stage(state_root=state).proposal.draft_contract == contract
    assert DomainContractV2.model_validate_json(contract.model_dump_json()) == contract
    provenance = next(item for item in prepared.proposal.assumptions if item.startswith("Design-first compilation: "))
    assert json.loads(provenance.split(": ", 1)[1])["compiler_capability"] == REVIEWED_DESIGN_64
    tampered = reviewed.model_dump(mode="json")
    tampered.pop("compiler_capability")
    with pytest.raises(ValidationError, match="hash mismatch"):
        DomainDesignEvaluation.model_validate(tampered)
    policy_tampered = contract.model_dump(mode="json")
    policy_tampered["reasoning_policy"]["retained_type_rationales"].pop(next(iter(contract.reasoning_policy.retained_type_rationales)))
    with pytest.raises(ValidationError, match="rationale for every type"):
        DomainContractV2.model_validate(policy_tampered)


@pytest.mark.parametrize("count,capability,succeeds", [(24, None, True), (25, None, False), (64, REVIEWED_DESIGN_64, True), (65, REVIEWED_DESIGN_64, False)])
def test_relationship_capacity_boundaries_across_compiler_layers(tmp_path, count, capability, succeeds):
    preflight = _preflight(tmp_path)
    raw = _sketch()
    raw["relationships"] = [
        {**raw["relationships"][0], "key": "describes" if index == 0 else f"relation_{index}"}
        for index in range(count)
    ]
    draft = generate_domain_design(preflight, client=Client(raw))
    evaluation = evaluate_domain_design(draft, compiler_capability=capability)
    compact = draft.sketch.model_dump(mode="json", exclude={"review_concerns"})
    for route in compact["question_routes"]:
        route.pop("unresolved_answer_requirements")
    compact_model = ReviewedCompactDesignSketch if capability else CompactDesignSketch
    if succeeds:
        compact_model.model_validate(compact)
        prepared = compile_domain_design(draft, evaluation, preflight=preflight)
        assert prepared.proposal.draft_contract.reasoning_policy.relationship_type_count == count
    else:
        with pytest.raises(ValidationError):
            compact_model.model_validate(compact)
        with pytest.raises(DesignCapabilityError):
            compile_domain_design(draft, evaluation, preflight=preflight)
        with pytest.raises(CompactDesignError, match="exceeds"):
            expand_compact_design(draft.sketch, intake=preflight.intake, known_evidence_ids=set(), compiler_capability=capability)

    # Independently exercise selection overflow, rather than only evaluator admission.
    one = expand_compact_design(
        generate_domain_design(preflight, client=Client(_sketch())).sketch,
        intake=preflight.intake, known_evidence_ids=set(),
    )
    candidates = [
        one.relationship_candidates[0].model_copy(update={
            "candidate_id": f"candidate:r{index}", "relationship_type_id": f"relationship-type:r{index}",
            "semantic_key": f"r{index}",
        })
        for index in range(count)
    ]
    if succeeds:
        result = select_relationship_vocabulary(candidates, one.question_routes, critical_question_ids=set(QUESTIONS), compiler_capability=capability)
        assert len(result.relationships) == count
    else:
        with pytest.raises(ProposalSelectionError, match="exceeds"):
            select_relationship_vocabulary(candidates, one.question_routes, critical_question_ids=set(QUESTIONS), compiler_capability=capability)


def test_policy_capability_is_explicit_not_an_arbitrary_numeric_override(tmp_path):
    base = {"relationship_type_count": 1, "max_hops": 1}
    legacy = ReasoningPolicyV2.model_validate(base)
    assert legacy.max_relationship_types == 24
    assert "compiler_capability" not in legacy.model_dump(mode="json")
    for additions in [
        {"max_relationship_types": 64},
        {"compiler_capability": REVIEWED_DESIGN_64},
        {"compiler_capability": "unbounded", "max_relationship_types": 64},
        {"compiler_capability": REVIEWED_DESIGN_64, "max_relationship_types": 65},
        {"relationship_type_count": 25, "max_relationship_types": 24},
        {"relationship_type_count": 65, "compiler_capability": REVIEWED_DESIGN_64, "max_relationship_types": 64},
    ]:
        with pytest.raises(ValidationError):
            ReasoningPolicyV2.model_validate({**base, **additions})
    with pytest.raises(L1StageError, match="model-free reviewed design"):
        prepare_l1_stage(_preflight(tmp_path), compiler_capability=REVIEWED_DESIGN_64)
    assert compact_design_schema()["properties"]["relationships"]["maxItems"] == 24
    assert compact_design_schema(compiler_capability=REVIEWED_DESIGN_64)["properties"]["relationships"]["maxItems"] == 64
    assert _selector_hash() == canonical_sha256({
        "selector_version": "l1-domain-selector/1.0.0",
        "policy": "minimum-cq-path-union-plus-mandatory-relationships;n-advisory-8-20-hard-24;k-shortest-max-4",
    })


def test_cli_capacity_requires_exact_evaluation_review(tmp_path):
    preflight = _preflight(tmp_path)
    raw, _ = _revision_nine_sketch()
    draft = generate_domain_design(preflight, client=Client(raw))
    path = tmp_path / "design.json"
    evaluation_path = tmp_path / "reviewed-evaluation.json"
    save_design_artifact(path, draft)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "domain", "evaluate-design", "--file", str(path), "--out", str(evaluation_path),
        "--compiler-capability", REVIEWED_DESIGN_64,
    ])
    assert result.exit_code == 0, result.output
    evaluation = json.loads(result.output)["result"]
    assert evaluation["compiler_capability"] == REVIEWED_DESIGN_64
    args = [
        "--dry-run", "domain", "compile-design", "--file", str(path),
        "--input", str(preflight.source_path), "--evaluation", str(evaluation_path),
        "--out-state", str(tmp_path / "cli-state"), "--out-domain", str(tmp_path / "cli-domain.yaml"),
    ]
    rejected = runner.invoke(cli, [*args, "--accept-evaluation-hash", evaluate_domain_design(draft).evaluation_hash])
    assert rejected.exit_code != 0 and "DESIGN_EVALUATION_REVIEW_MISMATCH" in rejected.output
    result = runner.invoke(cli, [*args, "--accept-evaluation-hash", evaluation["evaluation_hash"]])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["result"]["model_calls"] == 0
    assert not (tmp_path / "cli-state").exists()


def test_reviewed_capacity_preserves_other_compiler_gates(tmp_path):
    preflight = _preflight(tmp_path)
    raw = _sketch()
    raw["question_routes"][0]["source_key"] = "subject"
    raw["completeness"] = []
    draft = generate_domain_design(preflight, client=Client(raw))
    evaluation = evaluate_domain_design(draft, compiler_capability=REVIEWED_DESIGN_64)
    codes = {item.code for item in evaluation.compiler_limitations}
    assert {"compiler_zero_hop_query", "compiler_requires_explicit_completeness"} <= codes
    with pytest.raises(DesignCapabilityError):
        compile_domain_design(draft, evaluation, preflight=preflight)


def test_stored_reasoning_policy_schemas_match_updated_contract():
    root = Path(__file__).resolve().parents[2] / "src" / "fabric_kg_builder" / "domain"
    expected = DomainContractV2.model_json_schema()["$defs"]["ReasoningPolicyV2"]
    for path in [root / "domain.schema.json", root / "schemas" / "l1-domain-proposal-1.0.0.schema.json"]:
        assert json.loads(path.read_text())["$defs"]["ReasoningPolicyV2"] == expected
