"""A verified source schema can retain independent types without fabricated links."""

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain import window_schema_projection as projection
from fabric_kg_builder.domain.compact import expand_compact_design
from fabric_kg_builder.domain.design import load_domain_design
from fabric_kg_builder.domain.models import WindowSchemaProjectionBinding
from fabric_kg_builder.domain.proposal import build_draft_contract_from_candidates
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from tests.unit.test_domain_discovery_cli import _invoke
from tests.unit.test_window_run_approved_cli import _replay_args, _review
from tests.unit.test_window_schema_projection_cli import IsolatedSourceTypeModel, _approve_projected, original_case


@pytest.mark.parametrize("original_case", [IsolatedSourceTypeModel], indirect=True)
def test_isolated_source_type_and_property_survive_public_approval_and_l4(original_case, tmp_path, monkeypatch):
    source, windows, original, model = original_case
    original_bytes, calls = original.read_bytes(), len(model.calls)
    out = tmp_path / "projected.json"
    _invoke(["domain", "retain-window-schema", "--file", str(original), "--out", str(out), "--apply"])
    draft, parent = load_domain_design(out), load_domain_design(original)
    key = draft.schema_projection.retained_concept_keys["working:isolated-note"]
    property_key = draft.schema_projection.retained_concept_keys["working:isolated-voltage"]
    isolated = next(item for item in draft.sketch.types if item.key == key)
    assert not isolated.question_ids and not isolated.evidence_ids and not isolated.identity_property_keys
    assert all(key not in {item.source_key, item.target_key} for item in draft.sketch.relationships)
    assert draft.sketch.question_routes == parent.sketch.question_routes
    assert draft.sketch.completeness == parent.sketch.completeness
    before = out.read_bytes()
    stale_path = tmp_path / "old-evaluation.json"
    with monkeypatch.context() as patch:
        patch.setattr(projection, "projected_type_keys", lambda draft: set())
        stale = _invoke(["domain", "evaluate-design", "--file", str(out), "--out", str(stale_path)])
    assert any(item["code"] == "compiler_would_drop_types" for item in stale["result"]["compiler_limitations"])
    refused = CliRunner().invoke(cli, [
        "domain", "compile-design", "--input", str(source), "--file", str(out),
        "--evaluation", str(stale_path), "--accept-evaluation-hash", stale["result"]["evaluation_hash"],
        "--out-state", str(tmp_path / "stale-l1"), "--out-domain", str(tmp_path / "stale.yaml"),
    ])
    assert refused.exit_code != 0 and not (tmp_path / "stale.yaml").exists()
    domain = _approve_projected(source, out, tmp_path / "approved")
    contract = load_domain_contract(domain)
    type_id, property_id = "semantic-type:" + key, "property:" + property_key
    declared = next(item for item in contract.candidate_model.entity_types if item.type_id == type_id)
    assert any(item.property_id == property_id for item in declared.declared_properties)
    assert type_id in contract.window_schema_projection.required_retained_type_ids
    historical_binding = contract.window_schema_projection.model_dump(mode="python", exclude={"required_retained_type_ids"})
    assert WindowSchemaProjectionBinding.model_validate(historical_binding).model_dump(mode="python") == historical_binding
    assert all(type_id not in {*item.source_type_ids, *item.target_type_ids}
               for item in contract.candidate_model.relationship_types)
    review = tmp_path / "review.json"
    reviewed = _review(windows, domain, review)["result"]
    assert reviewed["concept_targets"]["working:isolated-note"] == type_id
    assert reviewed["concept_targets"]["working:isolated-voltage"] == property_id
    l1, l2, l3, l4 = tmp_path / "approved/l1", tmp_path / "l2", tmp_path / "l3", tmp_path / "l4"
    replay = _invoke(_replay_args(source, windows, domain, l1, l2, review))
    assert replay["l2_model_calls"] == 0
    for args in [
        ["validate-evidence", "--state", str(l3), "--l2-state", str(l2), "--l1-state", str(l1), "--domain", str(domain)],
        ["project-serving", "--state", str(l4), "--l3-state", str(l3), "--l2-state", str(l2), "--l1-state", str(l1), "--domain", str(domain)],
    ]:
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, result.output
    result = run_l4(run_l3(state_root=l3, l2_state_root=l2, l1_state_root=l1, domain_path=domain), state_root=l4)
    assert any(type_id in row["asserted_type_ids"] for row in result.rows.semantic_asserted_entities)
    assert any(row["semantic_property_id"] == property_id for row in result.rows.semantic_asserted_properties)
    assert result.rows.semantic_asserted_relationships
    assert original.read_bytes() == original_bytes and out.read_bytes() == before and len(model.calls) == calls


@pytest.mark.parametrize("original_case", [IsolatedSourceTypeModel], indirect=True)
def test_retention_requires_validated_projection_and_exact_semantic_candidates(original_case):
    parent = load_domain_design(original_case[2])
    draft = projection.retain_window_schema(parent)
    evidence = {item.evidence_span_id for item in draft.samples.evidence_spans}
    candidates = expand_compact_design(draft.sketch, intake=draft.inputs.intake, known_evidence_ids=evidence,
                                       derive_route_metadata=False)
    candidates = projection.project_compiler_policies(draft, candidates)
    type_id = "semantic-type:" + draft.schema_projection.retained_concept_keys["working:isolated-note"]
    ordinary, _, _ = build_draft_contract_from_candidates(
        draft.inputs.intake, candidates, known_evidence_span_ids=evidence,
    )
    assert type_id not in {item.type_id for item in ordinary.candidate_model.entity_types}
    retained, _, _ = build_draft_contract_from_candidates(
        draft.inputs.intake, candidates, known_evidence_span_ids=evidence, source_projection_draft=draft,
    )
    assert type_id in {item.type_id for item in retained.candidate_model.entity_types}
    assert retained.question_plans == ordinary.question_plans
    assert retained.completeness_requirements == ordinary.completeness_requirements
    assert retained.candidate_model.relationship_types == ordinary.candidate_model.relationship_types
    with pytest.raises(ValueError, match="EXACT_PROJECTED_DESIGN"):
        build_draft_contract_from_candidates(
            draft.inputs.intake, candidates, known_evidence_span_ids=evidence, source_projection_draft=parent,
        )
    changed = list(candidates.semantic_type_candidates)
    item = next(item for item in changed if item.proposed_type.type_id == type_id)
    changed[changed.index(item)] = item.model_copy(update={
        "proposed_type": item.proposed_type.model_copy(update={"description": "Unreviewed replacement."}),
    })
    with pytest.raises(ValueError, match="CANDIDATE_DRIFT"):
        build_draft_contract_from_candidates(
            draft.inputs.intake, candidates.model_copy(update={"semantic_type_candidates": tuple(changed)}),
            known_evidence_span_ids=evidence, source_projection_draft=draft,
        )
    from fabric_kg_builder.domain.design import design_preflight
    from fabric_kg_builder.domain.stage import prepare_l1_stage

    with pytest.raises(ValueError, match="full projected design proof"):
        prepare_l1_stage(
            design_preflight(draft, original_case[0]), candidates=candidates, client=None,
            design_window_run=draft.window_run, design_window_run_acceptance=draft.window_run_acceptance,
            design_schema_projection=projection.projection_contract_binding(draft),
            design_prompt_binding=("domain-design/" + draft.schema_projection.projection_version,
                                   draft.schema_projection.projection_hash),
        )
