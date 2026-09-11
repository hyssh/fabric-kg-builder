"""Retain the genuine working schema when the separate model draft omits it."""

import copy
import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.cli.domain_window_schema_projection_cmd import domain_retain_window_schema_cmd
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256, deterministic_contract_id
from fabric_kg_builder.domain.design import DomainDesignDraft, load_domain_design
from fabric_kg_builder.domain.discovery import prepare_discovery_corpus
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.domain.stage import preflight_l1_inputs
from fabric_kg_builder.domain.window_run import load_windowed_run
from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from fabric_kg_builder.sources.preparation import indexed_corpus_reader
from tests.unit.test_domain_discovery_cli import _invoke, _paths
from tests.unit.test_window_run_approved_cli import IntegratedModel, _replay_args, _review


class OmittedSourceModel(IntegratedModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            return result
        for candidate in result["candidates"]:
            if candidate["candidate_kind"] == "entity":
                candidate["observed_type"] = "Source Repair Step" if candidate["local_id"] == "record-1" else "Source Component"
            elif candidate["candidate_kind"] == "relationship":
                candidate["observed_predicate"] = "lift_out_of"
        for proposal in result["schema_proposals"]:
            concept = proposal["concept"]
            if concept["concept_id"] == "working:record":
                concept["name"] = "Source Repair Step"
            elif concept["concept_id"] == "working:subject":
                concept["name"] = "Source Component"
            elif concept["kind"] == "relationship":
                concept["name"] = "lift_out_of"
                concept["endpoint_policy"] = "allow_subtypes"
        wide = copy.deepcopy(result["candidates"][2])
        wide["observed_predicate"] = "shared_mount"
        index = len(result["candidates"])
        result["candidates"].append(wide)
        result["schema_proposals"].append({
            "action": "add_concept", "candidate_indices": [index], "reason": "Observed relation with an explicitly broad source scope.",
            "concept": {
                "concept_id": "working:wide", "kind": "relationship", "name": "shared_mount",
                "definition": "A shared mounting relation.",
                "source_type_ids": ["working:record", "working:subject"], "target_type_ids": ["working:subject"],
                "identity_policy": {"context_policy": "Source repair context"},
            },
        })
        return result


class CompatibleSourceModel(OmittedSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            result["types"][0]["display_name"] = "Source Repair Step"
            result["types"][0]["description"] = "A source service record."
            result["types"][1]["display_name"] = "Source Component"
            result["types"][1]["description"] = "A common service subject."
            result["relationships"][0]["display_name"] = "lift_out_of"
            result["relationships"][0]["description"] = "Record describes a subject."
        return result


class UnresolvedSourcePolicyModel(OmittedSourceModel):
    """Emit the explicit unresolved annotations used by the real bootstrap."""

    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" not in result:
            for proposal in result["schema_proposals"]:
                concept = proposal["concept"]
                if concept["kind"] == "property":
                    concept["identity_policy"] = {"mode": "unresolved"}
                elif concept["kind"] == "relationship":
                    concept["identity_policy"] = {"mode": "unresolved", "context_policy": "exact"}
        return result


class ResolvedSourcePolicyModel(UnresolvedSourcePolicyModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" not in result:
            for proposal in result["schema_proposals"]:
                if proposal["concept"]["kind"] in {"property", "relationship"}:
                    proposal["concept"]["identity_policy"]["mode"] = "business_key"
        return result


class DisjointRelationshipScopeModel(UnresolvedSourcePolicyModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" not in result:
            entity = copy.deepcopy(result["candidates"][0])
            entity.update(local_id="alternate-record", observed_type="Alternate Source Repair Step")
            relationship = copy.deepcopy(result["candidates"][2])
            relationship["source_local_id"] = "alternate-record"
            for candidate, concept in [
                (entity, {
                    "concept_id": "working:alternate-record", "kind": "entity",
                    "name": "Alternate Source Repair Step", "definition": "An alternate source repair step.",
                    "identity_policy": {"mode": "unresolved"},
                }),
                (relationship, {
                    "concept_id": "working:alternate-lift", "kind": "relationship", "name": "lift_out_of",
                    "definition": "An alternate step lifts the component.",
                    "source_type_ids": ["working:alternate-record"], "target_type_ids": ["working:subject"],
                    "identity_policy": {"mode": "unresolved", "context_policy": "exact"},
                }),
            ]:
                index = len(result["candidates"])
                result["candidates"].append(candidate)
                result["schema_proposals"].append({
                    "action": "add_concept", "concept": concept, "candidate_indices": [index],
                    "reason": "Keep a distinct observed source type and its exactly scoped relationship.",
                })
        return result


class OverlappingRelationshipScopeModel(CompatibleSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            subtype = copy.deepcopy(result["types"][0])
            subtype.update(key="subrecord", display_name="Child Repair Step", parent_key="record",
                           classification="domain_specialization", question_ids=[], evidence_ids=[])
            result["types"].append(subtype)
            relationship = copy.deepcopy(result["relationships"][0])
            relationship.update(key="child_lift", source_key="subrecord", question_ids=[], evidence_ids=[])
            result["relationships"].append(relationship)
        return result


class AliasRelationshipScopeModel(CompatibleSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            result["relationships"][0]["display_name"] = "LIFT_OUT_OF"
        return result


class ExtraSourcePolicyModel(UnresolvedSourcePolicyModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" not in result:
            for proposal in result["schema_proposals"]:
                if proposal["concept"]["kind"] in {"property", "relationship"}:
                    proposal["concept"]["identity_policy"]["property_keys"] = ["claimed_key"]
        return result


class IsolatedSourceTypeModel(UnresolvedSourcePolicyModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" not in result:
            entity = copy.deepcopy(result["candidates"][0])
            entity.update(local_id="isolated-note", observed_type="Source Safety Note")
            property_ = copy.deepcopy(result["candidates"][3])
            property_.update(owner_local_id="isolated-note", observed_property="Isolated Voltage")
            for candidate, concept in [
                (entity, {
                    "concept_id": "working:isolated-note", "kind": "entity", "name": "Source Safety Note",
                    "definition": "A source safety note with no declared relationships.",
                    "identity_policy": {"mode": "unresolved"},
                }),
                (property_, {
                    "concept_id": "working:isolated-voltage", "kind": "property", "name": "Isolated Voltage",
                    "definition": "Voltage recorded on the independent source note.",
                    "owner_type_ids": ["working:isolated-note"], "value_type": "number",
                    "identity_policy": {"mode": "unresolved"},
                }),
            ]:
                index = len(result["candidates"])
                result["candidates"].append(candidate)
                result["schema_proposals"].append({
                    "action": "add_concept", "concept": concept, "candidate_indices": [index],
                    "reason": "Retain a directly observed independent entity and property without inventing edges or CQ tags.",
                })
        return result


class DefinitionConflictModel(CompatibleSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            result["types"][1].update(display_name="Part", description="A financial ownership interest.")
            result["relationships"][0]["description"] = "Transfers financial ownership."
        else:
            for candidate in result["candidates"]:
                if candidate["candidate_kind"] == "entity" and candidate["local_id"] == "subject-1":
                    candidate["observed_type"] = "Part"
            for proposal in result["schema_proposals"]:
                concept = proposal["concept"]
                if concept["concept_id"] == "working:subject":
                    concept.update(name="Part", definition="A physical component of a device.")
                elif concept["concept_id"] == "working:describes":
                    concept["definition"] = "Lifts a physical component out of its assembly."
        return result


class RelationDefinitionConflictModel(CompatibleSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            result["relationships"][0]["description"] = "Transfers financial ownership."
        return result


class HierarchyConflictModel(CompatibleSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            result["types"][1].update(parent_key="record", classification="domain_specialization")
        return result


class EndpointConflictModel(CompatibleSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            result["relationships"][0].update(source_key="subject", target_key="record")
        return result


class DifferentPropertyOwnerModel(CompatibleSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            next(item for item in result["properties"] if item["display_name"] == "Observed Voltage")["owner_key"] = "record"
        return result


class ConflictingIdentityModel(CompatibleSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            result["types"][0]["identity_property_keys"] = ["invented_key"]
            result["properties"].append({
                "owner_key": result["types"][0]["key"], "key": "invented_key",
                "display_name": "Invented identity", "value_type": "string", "required": True,
            })
        return result


class StructuralReviewModel(OmittedSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            graph = [route for route in result["question_routes"] if route["source_key"] is not None]
            graph[0]["target_key"] = None
            for item in result["completeness"]:
                item["question_ids"] = [qid for qid in item["question_ids"] if qid != graph[1]["question_id"]]
            result["completeness"][0]["question_ids"].append(result["question_routes"][-1]["question_id"])
            result["relationships"][0]["question_ids"].append(result["question_routes"][-1]["question_id"])
        return result


@pytest.fixture(autouse=True)
def register_projection(monkeypatch):
    monkeypatch.setitem(cli.commands["domain"].commands, "retain-window-schema", domain_retain_window_schema_cmd)


@pytest.fixture
def original_case(tmp_path, request):
    source, intake, _, _, _ = _paths(tmp_path, count=1)
    next(source.glob("*.html")).write_text(
        "<p>A governed record describes a governed subject at 80.5 volts.</p>" * 3
    )
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=json.loads(intake.read_text()), project_id="project:records",
        run_id="run:source-preservation", model_version="offline", model_hash=canonical_sha256({"offline": True}),
    )
    prepared = prepare_discovery_corpus(
        preflight, reader=indexed_corpus_reader(preflight.corpus, source, project_id="project:records"),
    )
    prepared_path, windows, parent = tmp_path / "prepared.json", tmp_path / "windows", tmp_path / "original-design.json"
    prepared_path.write_text(canonical_json(prepared))
    model = getattr(request, "param", OmittedSourceModel)()
    _invoke([
        "domain", "window-run", "--prepared", str(prepared_path), "--intake", str(intake),
        "--out-state", str(windows), "--window-size", "1", "--concurrency", "1",
        "--max-calls", "1", "--max-repair-calls", "0", "--live",
    ], model=model)
    run = load_windowed_run(windows)
    assert run.cursor == 1 and run.state == "partial"
    assert {item.name for item in run.final_snapshot.concepts} >= {"lift_out_of", "shared_mount"}
    scope = tmp_path / "scope.json"
    _invoke([
        "domain", "accept-window-run-prefix", "--window-run", str(windows), "--out", str(scope),
        "--actor", "scope-reviewer", "--rationale", "Only this exact prefix is in scope.", "--accept",
    ])
    _invoke([
        "domain", "design", "--input", str(source), "--intake", str(intake), "--window-run", str(windows),
        "--window-run-acceptance", str(scope), "--out", str(parent), "--live",
    ], model=model)
    if type(model) is OmittedSourceModel:
        assert "lift_out_of" not in {item.display_name for item in load_domain_design(parent).sketch.relationships}
    return source, windows, parent, model


@pytest.mark.parametrize("original_case", [OmittedSourceModel, UnresolvedSourcePolicyModel], indirect=True)
def test_public_projection_retains_source_relationship_through_approved_l4(original_case, tmp_path):
    source, windows, original, model = original_case
    original_bytes = original.read_bytes()
    before = {path: path.read_bytes() for path in windows.rglob("*") if path.is_file()}
    model_calls = len(model.calls)
    out = tmp_path / "projected-design.json"
    args = ["domain", "retain-window-schema", "--file", str(original), "--out", str(out)]
    planned = _invoke(args)
    assert planned["status"] == "planned" and planned["writes"] == planned["model_calls"] == 0
    assert not out.exists() and "draft_hash" not in planned
    applied = _invoke([*args, "--apply"])
    assert applied["status"] == "projected" and applied["model_calls"] == 0
    draft, parent = load_domain_design(out), load_domain_design(original)
    assert draft.artifact_version == "8.0.0" and draft.schema_projection.model_call_count == draft.model_call_count == 0
    assert draft.schema_projection.parent_draft_hash == parent.draft_hash
    assert draft.schema_projection.scope_acceptance_hash == parent.window_run_acceptance.acceptance_hash
    assert draft.sketch.question_routes == parent.sketch.question_routes and draft.inputs == parent.inputs
    retained = draft.schema_projection.retained_concept_keys
    assert draft.schema_projection.projection_version == "window-schema-projection/1.2.0"
    assert draft.schema_projection.source_identity_policies == {
        concept.concept_id: concept.identity_policy for concept in draft.window_run.final_snapshot.concepts
    }
    if isinstance(model, UnresolvedSourcePolicyModel):
        assert draft.schema_projection.source_identity_policies["working:voltage"] == {"mode": "unresolved"}
        assert draft.schema_projection.source_identity_policies["working:describes"] == {
            "mode": "unresolved", "context_policy": "exact",
        }
    assert set(retained) == {"working:record", "working:subject", "working:describes", "working:voltage"}
    findings = draft.schema_projection.unsupported_findings
    assert len(findings) == 1 and findings[0].concept_id == "working:wide"
    assert findings[0].code == "multi_endpoint_relationship_not_representable"
    assert {item.display_name for item in draft.sketch.relationships} >= {"Describes", "lift_out_of"}
    for item in draft.sketch.types:
        if item.key in {retained["working:record"], retained["working:subject"]}:
            assert not item.identity_property_keys and not item.evidence_ids and not item.question_ids
    evaluated = _invoke(["domain", "evaluate-design", "--file", str(out), "--out", str(tmp_path / "evaluation.json")])
    assert any("multi_endpoint_relationship" in item["code"] for item in evaluated["result"]["findings"])
    l1, domain = tmp_path / "l1", tmp_path / "domain.yaml"
    compiled = _invoke([
        "domain", "compile-design", "--input", str(source), "--file", str(out),
        "--evaluation", str(tmp_path / "evaluation.json"), "--accept-evaluation-hash", evaluated["result"]["evaluation_hash"],
        "--out-state", str(l1), "--out-domain", str(domain),
    ])
    contract = load_domain_contract(domain)
    assert contract.approval.status != "approved"
    source_relation = next(item for item in contract.candidate_model.relationship_types if item.display_name == "lift_out_of")
    assert source_relation.source_type_ids == ["semantic-type:" + retained["working:record"]]
    assert source_relation.target_type_ids == ["semantic-type:" + retained["working:subject"]]
    assert source_relation.endpoint_policy == "allow_subtypes"
    working = next(item for item in draft.window_run.final_snapshot.concepts if item.name == "lift_out_of")
    assert source_relation.identity_policy.context_policy == working.identity_policy["context_policy"]
    approved = CliRunner().invoke(cli, [
        "domain", "approve", "--file", str(domain), "--state-dir", str(l1), "--approved-by", "source-reviewer",
        "--project-id", compiled["result"]["project_id"], "--run-id", compiled["result"]["run_id"],
        "--proposal-hash", compiled["result"]["proposal_hash"],
    ])
    assert approved.exit_code == 0, approved.output
    review = tmp_path / "mapping.json"
    mapping = _review(windows, domain, review)["result"]
    assert mapping["concept_targets"]["working:describes"] == source_relation.relationship_type_id
    assert mapping["pending_concept_ids"] == ["working:wide"]
    l2 = tmp_path / "l2"
    replay = _invoke(_replay_args(source, windows, domain, l1, l2, review))
    assert replay["l2_model_calls"] == 0 and replay["pending_chunks"] > 0
    l3, l4 = tmp_path / "l3", tmp_path / "l4"
    for args in [
        ["validate-evidence", "--state", str(l3), "--l2-state", str(l2), "--l1-state", str(l1), "--domain", str(domain)],
        ["project-serving", "--state", str(l4), "--l3-state", str(l3), "--l2-state", str(l2), "--l1-state", str(l1), "--domain", str(domain)],
    ]:
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, result.output
    result = run_l4(run_l3(state_root=l3, l2_state_root=l2, l1_state_root=l1, domain_path=domain), state_root=l4)
    assert result.rows.semantic_asserted_relationships and result.rows.semantic_asserted_properties
    assert all(row["semantic_relationship_id"] == source_relation.relationship_type_id for row in result.rows.semantic_asserted_relationships)
    assert len(model.calls) == model_calls and original.read_bytes() == original_bytes
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize("original_case", [ResolvedSourcePolicyModel, ExtraSourcePolicyModel], indirect=True)
def test_source_policy_support_does_not_erase_resolved_or_extra_identity_claims(original_case, tmp_path):
    source, windows, original, _ = original_case
    out = tmp_path / "projected.json"
    _invoke(["domain", "retain-window-schema", "--file", str(original), "--out", str(out), "--apply"])
    draft = load_domain_design(out)
    projection = draft.schema_projection
    assert {"working:voltage", "working:describes"}.isdisjoint(projection.retained_concept_keys)
    findings = {item.concept_id: item.code for item in projection.unsupported_findings}
    assert findings["working:voltage"] == "property_definition_not_representable"
    assert findings["working:describes"] == "relationship_identity_not_representable"
    assert projection.source_identity_policies == {
        concept.concept_id: concept.identity_policy for concept in draft.window_run.final_snapshot.concepts
    }
    domain = _approve_projected(source, out, tmp_path / "approved")
    reviewed = _review(windows, domain, tmp_path / "review.json")["result"]
    assert {"working:voltage", "working:describes"} <= set(reviewed["pending_concept_ids"])


@pytest.mark.parametrize("original_case", [UnresolvedSourcePolicyModel], indirect=True)
def test_policy_provenance_is_source_derived_and_historical_projection_remains_valid(original_case):
    from fabric_kg_builder.domain.window_schema_projection import _derive, retain_window_schema

    parent = load_domain_design(original_case[2])
    sketch, projection = _derive(parent, _projection_version="window-schema-projection/1.0.0")
    assert projection.source_identity_policies is None
    assert "source_identity_policies" not in projection.model_dump(mode="json")
    assert {"working:voltage", "working:describes"}.isdisjoint(projection.retained_concept_keys)
    values = parent.model_dump(mode="json", exclude={"draft_id", "draft_hash"})
    values.update(artifact_version="8.0.0", sketch=sketch.model_dump(mode="json"),
                  schema_projection=projection.model_dump(mode="json"), model_call_count=0)

    def sealed(values):
        digest = canonical_sha256(values)
        return canonical_json({
            **values, "draft_hash": digest,
            "draft_id": deterministic_contract_id("domain-design-draft", {"draft_hash": digest}),
        })

    historical = DomainDesignDraft.model_validate_json(sealed(values))
    assert historical.schema_projection == projection
    current = retain_window_schema(parent)
    assert {"working:voltage", "working:describes"} <= set(current.schema_projection.retained_concept_keys)
    values = current.model_dump(mode="json", exclude={"draft_id", "draft_hash"})
    provenance = values["schema_projection"]
    provenance["source_identity_policies"]["working:voltage"] = {}
    provenance["projection_hash"] = canonical_sha256({
        key: value for key, value in provenance.items() if key != "projection_hash"
    })
    with pytest.raises(ValueError, match="DERIVATION_MISMATCH"):
        DomainDesignDraft.model_validate_json(sealed(values))


@pytest.mark.parametrize("original_case", [DisjointRelationshipScopeModel], indirect=True)
def test_same_name_disjoint_source_scopes_remain_distinct_through_l4(original_case, tmp_path):
    from fabric_kg_builder.domain.window_schema_projection import _derive

    source, windows, original, model = original_case
    original_bytes, calls = original.read_bytes(), len(model.calls)
    parent = load_domain_design(original)
    old_sketch, historical = _derive(parent, _projection_version="window-schema-projection/1.1.0")
    source_ids = {"working:describes", "working:alternate-lift"}
    assert len(source_ids & set(historical.retained_concept_keys)) == 1
    old_values = parent.model_dump(mode="json", exclude={"draft_id", "draft_hash"})
    old_values.update(artifact_version="8.0.0", sketch=old_sketch.model_dump(mode="json"),
                      schema_projection=historical.model_dump(mode="json"), model_call_count=0)
    old_hash = canonical_sha256(old_values)
    assert DomainDesignDraft.model_validate_json(canonical_json({
        **old_values, "draft_hash": old_hash,
        "draft_id": deterministic_contract_id("domain-design-draft", {"draft_hash": old_hash}),
    })).schema_projection == historical
    out = tmp_path / "scoped.json"
    _invoke(["domain", "retain-window-schema", "--file", str(original), "--out", str(out), "--apply"])
    draft = load_domain_design(out)
    keys = {draft.schema_projection.retained_concept_keys[item] for item in source_ids}
    assert len(keys) == 2
    relationships = [item for item in draft.sketch.relationships if item.key in keys]
    assert {item.display_name for item in relationships} == {"lift_out_of"}
    assert len({item.source_key for item in relationships}) == 2
    assert len({item.target_key for item in relationships}) == 1
    domain = _approve_projected(source, out, tmp_path / "approved")
    contract = load_domain_contract(domain)
    compiled = [item for item in contract.candidate_model.relationship_types if item.display_name == "lift_out_of"]
    assert {item.relationship_type_id for item in compiled} == {"relationship-type:" + key for key in keys}
    assert len({tuple(item.source_type_ids) for item in compiled}) == 2
    from fabric_kg_builder.enrichment.schema2_extraction import compile_closed_vocabulary
    from fabric_kg_builder.enrichment.window_run_reuse import projected_id_only_relationship_aliases

    vocabulary = compile_closed_vocabulary(contract)
    assert "lift_out_of" not in vocabulary.relationships_by_alias
    assert all(item.relationship_type_id.casefold() in vocabulary.relationships_by_alias for item in compiled)
    assert vocabulary.prompt_payload["id_only_relationship_display_names"] == ["lift_out_of"]
    unprojected = contract.model_copy(update={"window_schema_projection": None})
    assert projected_id_only_relationship_aliases(unprojected) == set()
    with pytest.raises(ValueError, match="ambiguous approved relationship alias"):
        compile_closed_vocabulary(unprojected)
    review = tmp_path / "review.json"
    reviewed = _review(windows, domain, review)["result"]
    assert len({reviewed["concept_targets"][item] for item in source_ids}) == 2
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
    assert {row["semantic_relationship_id"] for row in result.rows.semantic_asserted_relationships} == {
        item.relationship_type_id for item in compiled
    }
    assert result.rows.semantic_asserted_properties
    assert len(model.calls) == calls and original.read_bytes() == original_bytes


@pytest.mark.parametrize("original_case", [OverlappingRelationshipScopeModel, AliasRelationshipScopeModel], indirect=True)
def test_relationship_overlap_and_spelling_ambiguity_still_require_review(original_case, tmp_path):
    parent = load_domain_design(original_case[2])
    out = tmp_path / "ambiguous.json"
    _invoke([
        "domain", "retain-window-schema", "--file", str(original_case[2]), "--out", str(out), "--apply",
        "--prefer-window-definitions", "--actor", "reviewer", "--rationale", "Definitions only, never scopes.",
    ])
    result = load_domain_design(out)
    assert "working:describes" not in result.schema_projection.retained_concept_keys
    assert any(item.concept_id == "working:describes" and item.code == "existing_relationship_conflict"
               for item in result.schema_projection.unsupported_findings)
    assert result.sketch.relationships == parent.sketch.relationships


def test_projection_refuses_fake_derivation_and_dry_run_apply(original_case, tmp_path):
    _, _, original, _ = original_case
    out = tmp_path / "projected.json"
    args = ["domain", "retain-window-schema", "--file", str(original), "--out", str(out)]
    conflict = CliRunner().invoke(cli, ["--dry-run", *args, "--apply"])
    assert conflict.exit_code == 2 and not out.exists()
    _invoke([*args, "--apply"])
    draft = load_domain_design(out)
    for field in ("derived_concept_ids", "snapshot_hash", "scope_acceptance_hash"):
        values = draft.model_dump(mode="json")
        values["schema_projection"][field] = [] if field == "derived_concept_ids" else "0" * 64
        values["schema_projection"]["projection_hash"] = canonical_sha256({
            key: value for key, value in values["schema_projection"].items() if key != "projection_hash"
        })
        values["draft_hash"] = canonical_sha256({key: value for key, value in values.items() if key not in {"draft_hash", "draft_id"}})
        values["draft_id"] = deterministic_contract_id("domain-design-draft", {"draft_hash": values["draft_hash"]})
        with pytest.raises(ValueError, match="DERIVATION_MISMATCH"):
            DomainDesignDraft.model_validate_json(canonical_json(values))
    values = draft.model_dump(mode="json")
    values["sketch"]["relationships"][-1]["display_name"] = "fabricated replacement"
    values["draft_hash"] = canonical_sha256({key: value for key, value in values.items() if key not in {"draft_hash", "draft_id"}})
    values["draft_id"] = deterministic_contract_id("domain-design-draft", {"draft_hash": values["draft_hash"]})
    with pytest.raises(ValueError, match="DERIVATION_MISMATCH"):
        DomainDesignDraft.model_validate_json(canonical_json(values))
    repeated = CliRunner().invoke(cli, [
        "domain", "retain-window-schema", "--file", str(out), "--out", str(tmp_path / "nested.json"), "--apply",
    ])
    assert repeated.exit_code != 0 and "original integrated model draft" in repeated.output


@pytest.mark.parametrize("original_case", [CompatibleSourceModel], indirect=True)
def test_exact_compatible_names_reuse_existing_schema_keys(original_case, tmp_path):
    _, _, original, _ = original_case
    out = tmp_path / "retained.json"
    _invoke(["domain", "retain-window-schema", "--file", str(original), "--out", str(out), "--apply"])
    parent, draft = load_domain_design(original), load_domain_design(out)
    assert draft.schema_projection.derived_concept_ids == []
    assert draft.sketch.types == parent.sketch.types and draft.sketch.properties == parent.sketch.properties
    assert draft.sketch.relationships == parent.sketch.relationships
    assert len(draft.schema_projection.retained_concept_keys) == 4


@pytest.mark.parametrize("original_case", [ConflictingIdentityModel], indirect=True)
def test_conflicting_identity_is_reported_without_merge_or_downgrade(original_case, tmp_path):
    _, _, original, _ = original_case
    out = tmp_path / "retained.json"
    _invoke(["domain", "retain-window-schema", "--file", str(original), "--out", str(out), "--apply"])
    parent, draft = load_domain_design(original), load_domain_design(out)
    assert draft.sketch.types == parent.sketch.types
    assert draft.sketch.types[0].identity_property_keys == ["invented_key"]
    assert "working:record" not in draft.schema_projection.retained_concept_keys
    assert any(item.concept_id == "working:record" and item.code == "existing_type_conflict"
               for item in draft.schema_projection.unsupported_findings)
    preferred = tmp_path / "still-conflicting.json"
    _invoke([
        "domain", "retain-window-schema", "--file", str(original), "--out", str(preferred), "--apply",
        "--prefer-window-definitions", "--actor", "reviewer", "--rationale", "Only prefer definitions, never identities.",
    ])
    preferred_draft = load_domain_design(preferred)
    assert "working:record" not in preferred_draft.schema_projection.retained_concept_keys
    assert preferred_draft.sketch.types[0].identity_property_keys == ["invented_key"]


def _approve_projected(source, file, root):
    root.mkdir()
    evaluation = root / "evaluation.json"
    result = _invoke(["domain", "evaluate-design", "--file", str(file), "--out", str(evaluation)])
    l1, domain = root / "l1", root / "domain.yaml"
    compiled = _invoke([
        "domain", "compile-design", "--input", str(source), "--file", str(file), "--evaluation", str(evaluation),
        "--accept-evaluation-hash", result["result"]["evaluation_hash"], "--out-state", str(l1), "--out-domain", str(domain),
    ])
    approved = CliRunner().invoke(cli, [
        "domain", "approve", "--file", str(domain), "--state-dir", str(l1), "--approved-by", "reviewer",
        "--project-id", compiled["result"]["project_id"], "--run-id", compiled["result"]["run_id"],
        "--proposal-hash", compiled["result"]["proposal_hash"],
    ])
    assert approved.exit_code == 0, approved.output
    return domain


@pytest.mark.parametrize("original_case", [DefinitionConflictModel, RelationDefinitionConflictModel], indirect=True)
def test_definition_conflicts_require_explicit_precedence_and_gate_mapping(original_case, tmp_path):
    source, windows, original, model = original_case
    original_bytes, calls = original.read_bytes(), len(model.calls)
    parent = load_domain_design(original)
    conflict_id = "working:subject" if isinstance(model, DefinitionConflictModel) else "working:describes"
    out = tmp_path / "default.json"
    _invoke(["domain", "retain-window-schema", "--file", str(original), "--out", str(out), "--apply"])
    default = load_domain_design(out)
    assert conflict_id not in default.schema_projection.retained_concept_keys
    assert any(item.concept_id == conflict_id and item.code == "existing_definition_conflict"
               for item in default.schema_projection.unsupported_findings)
    assert default.sketch.types == parent.sketch.types and default.sketch.relationships == parent.sketch.relationships
    default_domain = _approve_projected(source, out, tmp_path / "default-approved")
    review = _review(windows, default_domain, tmp_path / "default-review.json")["result"]
    assert conflict_id not in review["concept_targets"] and conflict_id in review["pending_concept_ids"]
    assert conflict_id in load_domain_contract(default_domain).window_schema_projection.unsupported_concepts
    preferred = tmp_path / "preferred.json"
    args = ["domain", "retain-window-schema", "--file", str(original), "--out", str(preferred),
            "--prefer-window-definitions", "--apply"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 2 and not preferred.exists()
    _invoke([*args, "--actor", "source-reviewer", "--rationale", "Explicit current-window schema precedence."])
    chosen = load_domain_design(preferred)
    changes = chosen.schema_projection.definition_precedence
    assert {change.concept_id for change in changes} == (
        {"working:subject", "working:describes"} if isinstance(model, DefinitionConflictModel) else {"working:describes"}
    )
    assert chosen.schema_projection.parent_sketch == parent.sketch
    assert chosen.schema_projection.operator_corrections.prefer_window_definitions is True
    source_concepts = {item.concept_id: item for item in chosen.window_run.final_snapshot.concepts}
    for change in changes:
        assert change.source_definition == source_concepts[change.concept_id].definition
        assert change.source_definition_hash == canonical_sha256(change.source_definition)
        assert change.parent_definition_hash == canonical_sha256(change.parent_definition)
        items = chosen.sketch.types if change.kind == "entity" else chosen.sketch.relationships
        original_items = parent.sketch.types if change.kind == "entity" else parent.sketch.relationships
        item = next(item for item in items if item.key == change.schema_key)
        old = next(item for item in original_items if item.key == change.schema_key)
        assert item.description == change.source_definition and old.description == change.parent_definition
        assert item.model_dump(exclude={"description"}) == old.model_dump(exclude={"description"})
    preferred_domain = _approve_projected(source, preferred, tmp_path / "preferred-approved")
    review = _review(windows, preferred_domain, tmp_path / "preferred-review.json")["result"]
    assert conflict_id in review["concept_targets"] and conflict_id not in review["pending_concept_ids"]
    assert original.read_bytes() == original_bytes and len(model.calls) == calls
    values = chosen.model_dump(mode="json")
    values["schema_projection"]["definition_precedence"][0]["parent_definition_hash"] = "0" * 64
    with pytest.raises(ValueError, match="PRECEDENCE_HASH_MISMATCH"):
        DomainDesignDraft.model_validate_json(canonical_json(values))


@pytest.mark.parametrize("original_case", [HierarchyConflictModel, EndpointConflictModel, DifferentPropertyOwnerModel], indirect=True)
def test_definition_preference_never_overrides_structural_scopes(original_case, tmp_path):
    _, _, original, model = original_case
    out = tmp_path / "preferred.json"
    _invoke([
        "domain", "retain-window-schema", "--file", str(original), "--out", str(out), "--apply",
        "--prefer-window-definitions", "--actor", "reviewer", "--rationale", "Definitions only.",
    ])
    parent, result = load_domain_design(original), load_domain_design(out)
    assert result.schema_projection.definition_precedence == []
    assert result.sketch.types == parent.sketch.types
    if isinstance(model, EndpointConflictModel):
        assert all(item in result.sketch.relationships for item in parent.sketch.relationships)
        key = result.schema_projection.retained_concept_keys["working:describes"]
        added = next(item for item in result.sketch.relationships if item.key == key)
        assert (added.source_key, added.target_key) == ("record", "subject")
        assert (parent.sketch.relationships[0].source_key, parent.sketch.relationships[0].target_key) == ("subject", "record")
        return
    assert result.sketch.relationships == parent.sketch.relationships
    if isinstance(model, DifferentPropertyOwnerModel):
        key = result.schema_projection.retained_concept_keys["working:voltage"]
        assert key.startswith("subject.")
        assert any(item.owner_key == "record" and item.display_name == "Observed Voltage" for item in result.sketch.properties)
    else:
        missing = "working:subject" if isinstance(model, HierarchyConflictModel) else "working:describes"
        assert missing not in result.schema_projection.retained_concept_keys


@pytest.mark.parametrize("original_case", [StructuralReviewModel], indirect=True)
def test_explicit_structural_corrections_are_audited_and_still_require_evaluation(original_case, tmp_path):
    source, _, original, model = original_case
    parent = load_domain_design(original)
    original_bytes, calls = original.read_bytes(), len(model.calls)
    graph = [route for route in parent.sketch.question_routes if route.source_key is not None]
    first, second = graph[:2]
    before = _invoke(["domain", "evaluate-design", "--file", str(original), "--out", str(tmp_path / "before-evaluation.json")])
    assert before["result"]["compiler_limitations"]
    declaration = {
        "key": "explicit_subject_collection", "relationship_key": parent.sketch.relationships[0].key,
        "kind": "collection", "question_ids": [second.question_id],
        "ordered": False, "ordinal_property_key": None, "rationale": "Explicit unordered schema collection; no observed member count.",
    }
    patch = tmp_path / "completeness.json"
    patch.write_text(canonical_json(declaration))
    out = tmp_path / "corrected.json"
    args = [
        "domain", "retain-window-schema", "--file", str(original), "--out", str(out),
        "--route-target", f"{first.question_id}={second.target_key}", "--completeness", str(patch),
        "--actor", "structural-reviewer", "--rationale", "Resolve two explicit structural review decisions.",
    ]
    preview = _invoke(args)
    assert preview["writes"] == 0 and not out.exists()
    assert preview["operator_corrections"]["authority"] == "explicit_unapproved_schema_corrections"
    _invoke([*args, "--apply"])
    corrected = load_domain_design(out)
    corrections = corrected.schema_projection.operator_corrections
    assert corrections.actor == "structural-reviewer"
    assert corrections.route_targets == {first.question_id: second.target_key}
    assert corrected.inputs == parent.inputs
    for old, new in zip(parent.sketch.question_routes, corrected.sketch.question_routes):
        if old.question_id == first.question_id:
            assert new == old.model_copy(update={"target_key": second.target_key})
        else:
            assert new == old
    assert corrected.sketch.question_routes[-1].source_key is corrected.sketch.question_routes[-1].target_key is None
    assert corrected.sketch.completeness[:-1] == parent.sketch.completeness
    assert corrected.sketch.question_routes[-1].question_id in corrected.sketch.completeness[0].question_ids
    assert corrected.sketch.completeness[-1].model_dump(mode="json") == declaration
    stale = CliRunner().invoke(cli, [
        "domain", "compile-design", "--input", str(source), "--file", str(out),
        "--evaluation", str(tmp_path / "before-evaluation.json"),
        "--accept-evaluation-hash", before["result"]["evaluation_hash"],
        "--out-state", str(tmp_path / "stale-l1"), "--out-domain", str(tmp_path / "stale-domain.yaml"),
    ])
    assert stale.exit_code != 0
    evaluated = _invoke(["domain", "evaluate-design", "--file", str(out), "--out", str(tmp_path / "after-evaluation.json")])
    assert evaluated["result"]["compiler_limitations"] == []
    l1, domain = tmp_path / "corrected-l1", tmp_path / "corrected-domain.yaml"
    compiled = _invoke([
        "domain", "compile-design", "--input", str(source), "--file", str(out),
        "--evaluation", str(tmp_path / "after-evaluation.json"),
        "--accept-evaluation-hash", evaluated["result"]["evaluation_hash"],
        "--out-state", str(l1), "--out-domain", str(domain),
    ])
    assert load_domain_contract(domain).approval.status != "approved"
    approved = CliRunner().invoke(cli, [
        "domain", "approve", "--file", str(domain), "--state-dir", str(l1), "--approved-by", "reviewer",
        "--project-id", compiled["result"]["project_id"], "--run-id", compiled["result"]["run_id"],
        "--proposal-hash", compiled["result"]["proposal_hash"],
    ])
    assert approved.exit_code == 0, approved.output
    assert original.read_bytes() == original_bytes and len(model.calls) == calls
    values = corrected.model_dump(mode="json")
    changes = values["schema_projection"]["operator_corrections"]
    changes["route_targets"][first.question_id] = first.source_key
    changes["correction_hash"] = canonical_sha256({key: value for key, value in changes.items() if key != "correction_hash"})
    projection = values["schema_projection"]
    projection["projection_hash"] = canonical_sha256({key: value for key, value in projection.items() if key != "projection_hash"})
    values["draft_hash"] = canonical_sha256({key: value for key, value in values.items() if key not in {"draft_hash", "draft_id"}})
    values["draft_id"] = deterministic_contract_id("domain-design-draft", {"draft_hash": values["draft_hash"]})
    with pytest.raises(ValueError, match="DERIVATION_MISMATCH"):
        DomainDesignDraft.model_validate_json(canonical_json(values))


def test_corrections_reject_unknown_refs_sql_edits_and_untyped_counts(original_case, tmp_path):
    _, _, original, _ = original_case
    parent = load_domain_design(original)
    graph, sql = parent.sketch.question_routes[0], parent.sketch.question_routes[-1]
    base = ["domain", "retain-window-schema", "--file", str(original), "--out", str(tmp_path / "refused.json"), "--apply"]
    missing_actor = CliRunner().invoke(cli, [*base, "--route-target", f"{graph.question_id}={graph.target_key}"])
    assert missing_actor.exit_code == 2 and "--actor and --rationale" in missing_actor.output
    options = ["--actor", "reviewer", "--rationale", "Explicit correction"]
    for target, expected in [
        ("cq:unknown=subject", "UNKNOWN_ROUTE_REFERENCE"),
        (f"{graph.question_id}=unknown_type", "UNKNOWN_ROUTE_REFERENCE"),
        (f"{sql.question_id}={graph.target_key}", "SQL_ROUTE_MUST_REMAIN_NULL"),
    ]:
        result = CliRunner().invoke(cli, [*base, *options, "--route-target", target])
        assert result.exit_code != 0 and expected in result.output
    for update in ({"relationship_key": "missing_relationship"}, {"observed_count": 9}, {}):
        declaration = parent.sketch.completeness[0].model_dump(mode="json")
        declaration.update(update)
        patch = tmp_path / "invalid-completeness.json"
        patch.write_text(canonical_json(declaration))
        result = CliRunner().invoke(cli, [*base, *options, "--completeness", str(patch)])
        assert result.exit_code != 0 and not (tmp_path / "refused.json").exists()
