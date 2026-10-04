"""Reviewed identity correction is a model-free, replay-validated draft operation."""

import copy

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256, deterministic_contract_id
from fabric_kg_builder.domain.design import DomainDesignDraft, WindowDesignCorrections, load_domain_design
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.domain.window_schema_projection import (
    _derive, _source_scoped_sketch, build_design_corrections, retain_window_schema,
)
from tests.unit.test_domain_discovery_cli import _invoke
from tests.unit.test_window_run_approved_cli import _review
from tests.unit.test_window_schema_projection_cli import (
    CompatibleSourceModel, original_case, register_projection,
)


class DescriptiveIdentityModel(CompatibleSourceModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        names = {"Source Repair Step": "Model", "Source Component": "Procedure"}
        if "types" in result:
            for item in result["types"]:
                item["display_name"] = names[item["display_name"]]
                item["description"] = "Model paraphrase requiring explicit source-definition precedence."
                item["identity_property_keys"] = [item["key"] + ".variant"]
                result["properties"].append({
                    "owner_key": item["key"], "key": "variant", "display_name": "Variant",
                    "value_type": "string", "required": False,
                })
            unselected = copy.deepcopy(result["types"][0])
            unselected.update(key="unselected", display_name="Unselected", identity_property_keys=["variant"],
                              question_ids=[], evidence_ids=[])
            result["types"].append(unselected)
            result["properties"].append({
                "owner_key": "unselected", "key": "variant", "display_name": "Variant",
                "value_type": "string", "required": False,
            })
            relationship = copy.deepcopy(result["relationships"][0])
            relationship.update(key="unselected_link", display_name="Unselected Link", target_key="unselected")
            result["relationships"].append(relationship)
        else:
            for candidate in result["candidates"]:
                if candidate["candidate_kind"] == "entity":
                    candidate["observed_type"] = names.get(candidate["observed_type"], candidate["observed_type"])
            for proposal in result["schema_proposals"]:
                concept = proposal["concept"]
                if concept["kind"] == "entity":
                    concept["name"] = names.get(concept["name"], concept["name"])
        return result


class AmbiguousDraftIdentityModel(DescriptiveIdentityModel):
    def complete_json(self, **request):
        result = super().complete_json(**request)
        if "types" in result:
            result["types"][-1]["display_name"] = "MODEL"
        return result


def _corrections(names=("Model", "Procedure"), **kwargs):
    return build_design_corrections(
        actor="identity-reviewer", rationale="Variants are optional descriptions, not source identities.",
        route_targets={}, completeness=[], source_scoped_types=names, **kwargs,
    )


def _args(original, out):
    return ["domain", "retain-window-schema", "--file", str(original), "--out", str(out)]


def _reseal(values):
    projection = values["schema_projection"]
    projection["projection_hash"] = canonical_sha256({
        key: value for key, value in projection.items() if key != "projection_hash"
    })
    values["draft_hash"] = canonical_sha256({
        key: value for key, value in values.items() if key not in {"draft_hash", "draft_id"}
    })
    values["draft_id"] = deterministic_contract_id("domain-design-draft", {"draft_hash": values["draft_hash"]})
    return canonical_json(values)


@pytest.mark.parametrize("original_case", [DescriptiveIdentityModel], indirect=True)
def test_reviewed_source_identity_retains_properties_endpoints_and_compiles_unapproved(original_case, tmp_path):
    source, windows, original, model = original_case
    original_bytes, calls = original.read_bytes(), len(model.calls)
    parent = load_domain_design(original)
    before = parent.model_dump(mode="json")
    blocked = retain_window_schema(parent)
    assert {"working:record", "working:subject", "working:describes"}.isdisjoint(
        blocked.schema_projection.retained_concept_keys
    )
    out = tmp_path / "corrected.json"
    args = [
        *_args(original, out), "--source-scoped-type", "Model", "--source-scoped-type", "Procedure",
        "--prefer-window-definitions", "--actor", "identity-reviewer",
        "--rationale", "Variants are optional descriptions, not source identities.",
    ]
    preview = _invoke(args)
    assert preview["writes"] == 0 and not out.exists()
    _invoke([*args, "--apply"])
    draft = load_domain_design(out)
    projection = draft.schema_projection
    assert projection.projection_version == "window-schema-projection/1.3.0"
    assert projection.parent_sketch == parent.sketch
    assert projection.parent_draft_hash == parent.draft_hash
    assert projection.operator_corrections.source_scoped_types == ("Model", "Procedure")
    assert projection.operator_corrections.correction_version == "window-design-corrections/1.1.0"
    assert projection.approval_inherited is False
    for old, new in zip(parent.sketch.types, draft.sketch.types):
        if old.display_name in {"Model", "Procedure"}:
            assert old.identity_property_keys and new.identity_property_keys == []
            assert new.model_dump(exclude={"identity_property_keys", "description"}) == old.model_dump(
                exclude={"identity_property_keys", "description"}
            )
        else:
            assert old == new
    assert draft.sketch.properties == parent.sketch.properties
    assert all(not item.required for item in draft.sketch.properties if item.key == "variant")
    assert draft.sketch.relationships == parent.sketch.relationships
    assert projection.retained_concept_keys["working:record"] == "record"
    assert projection.retained_concept_keys["working:subject"] == "subject"
    assert "working:describes" in projection.retained_concept_keys
    assert [item.concept_id for item in projection.unsupported_findings] == ["working:wide"]
    assert retain_window_schema(parent, corrections=_corrections(prefer_window_definitions=True)) == draft
    assert DomainDesignDraft.model_validate_json(canonical_json(draft)) == draft
    assert parent.model_dump(mode="json") == before and original.read_bytes() == original_bytes
    assert len(model.calls) == calls
    evaluation = tmp_path / "evaluation.json"
    evaluated = _invoke(["domain", "evaluate-design", "--file", str(out), "--out", str(evaluation)])
    domain, l1 = tmp_path / "domain.yaml", tmp_path / "l1"
    compiled = _invoke([
        "domain", "compile-design", "--input", str(source), "--file", str(out),
        "--evaluation", str(evaluation), "--accept-evaluation-hash", evaluated["result"]["evaluation_hash"],
        "--out-state", str(l1), "--out-domain", str(domain),
    ])
    contract = load_domain_contract(domain)
    assert contract.approval.status != "approved"
    for entity in contract.candidate_model.entity_types:
        if entity.display_name in {"Model", "Procedure"}:
            assert entity.identity_key_policy.key_mode == "stable_source_identity"
            assert entity.identity_key_policy.business_key_fields == []
    approved = CliRunner().invoke(cli, [
        "domain", "approve", "--file", str(domain), "--state-dir", str(l1), "--approved-by", "reviewer",
        "--project-id", compiled["result"]["project_id"], "--run-id", compiled["result"]["run_id"],
        "--proposal-hash", compiled["result"]["proposal_hash"],
    ])
    assert approved.exit_code == 0, approved.output
    reviewed = _review(windows, domain, tmp_path / "mapping.json")["result"]
    assert reviewed["concept_targets"]["working:record"] == "semantic-type:record"
    assert reviewed["concept_targets"]["working:subject"] == "semantic-type:subject"
    assert "working:describes" in reviewed["concept_targets"]
    refused = CliRunner().invoke(cli, [
        *_args(domain, tmp_path / "approved-identity-bypass.json"),
        "--source-scoped-type", "Model", "--actor", "reviewer", "--rationale", "Not a draft.", "--apply",
    ])
    assert refused.exit_code != 0
    assert not (tmp_path / "approved-identity-bypass.json").exists()
    assert original.read_bytes() == original_bytes and len(model.calls) == calls


@pytest.mark.parametrize("original_case", [DescriptiveIdentityModel], indirect=True)
def test_source_identity_cli_requires_review_and_exact_references(original_case, tmp_path):
    original = original_case[2]
    out = tmp_path / "refused.json"
    base = [*_args(original, out), "--apply"]
    for options in ([], ["--actor", "reviewer"], ["--rationale", "Reviewed"],
                    ["--actor", " ", "--rationale", "Reviewed"], ["--actor", "reviewer", "--rationale", " "]):
        result = CliRunner().invoke(cli, [*base, "--source-scoped-type", "Model", *options])
        assert result.exit_code == 2 and "--actor and --rationale" in result.output
    reviewed = [*base, "--actor", "reviewer", "--rationale", "Explicit review"]
    for names, expected in [
        (["Missing"], "TYPE_NOT_UNIQUE"), (["model"], "TYPE_NOT_UNIQUE"),
        (["record"], "TYPE_NOT_UNIQUE"), (["Unselected"], "TYPE_NOT_UNIQUE"),
        (["Model", "Model"], "DUPLICATE_SOURCE_SCOPED_TYPE"),
        (["Model", "model"], "DUPLICATE_SOURCE_SCOPED_TYPE"),
        (["Model"], "TYPE_NOT_RETAINED"),
    ]:
        options = [arg for name in names for arg in ("--source-scoped-type", name)]
        result = CliRunner().invoke(cli, [*reviewed, *options])
        assert result.exit_code != 0 and expected in result.output, result.output
        assert not out.exists()


@pytest.mark.parametrize("original_case", [AmbiguousDraftIdentityModel], indirect=True)
def test_source_identity_cli_rejects_ambiguous_draft_names(original_case, tmp_path):
    out = tmp_path / "refused.json"
    result = CliRunner().invoke(cli, [
        *_args(original_case[2], out), "--source-scoped-type", "Model", "--apply",
        "--actor", "reviewer", "--rationale", "Explicit review", "--prefer-window-definitions",
    ])
    assert result.exit_code != 0 and "TYPE_NOT_UNIQUE" in result.output and not out.exists()


@pytest.mark.parametrize("original_case", [DescriptiveIdentityModel], indirect=True)
def test_source_identity_guard_rejects_resolved_seed_policies_and_ambiguous_sources(original_case):
    parent = load_domain_design(original_case[2])
    concepts = list(parent.window_run.final_snapshot.concepts)
    index = next(i for i, item in enumerate(concepts) if item.name == "Model")
    for policy in ({}, {"mode": "business_key"}, {"mode": "source_scoped"},
                   {"mode": "unresolved", "keys": ["variant"]},
                   {"identity_root_type_id": "working:record", "key_policy": {"key_mode": "source_scoped"}}):
        changed = list(concepts)
        changed[index] = concepts[index].model_copy(update={"identity_policy": policy})
        with pytest.raises(ValueError, match="SOURCE_IDENTITY_NOT_UNRESOLVED"):
            _source_scoped_sketch(parent.sketch, changed, ("Model",))
    for duplicate_name in ("Model", "MODEL"):
        duplicate = concepts[index].model_copy(update={"concept_id": "working:duplicate", "name": duplicate_name})
        with pytest.raises(ValueError, match="TYPE_NOT_UNIQUE"):
            _source_scoped_sketch(parent.sketch, [*concepts, duplicate], ("Model",))
    child = parent.sketch.types[-1].model_copy(update={"parent_key": "record", "identity_property_keys": []})
    sketch = parent.sketch.model_copy(update={"types": [*parent.sketch.types[:-1], child]})
    with pytest.raises(ValueError, match="UNSELECTED_INHERITED_IDENTITY"):
        _source_scoped_sketch(sketch, concepts, ("Model",))


@pytest.mark.parametrize("original_case", [DescriptiveIdentityModel], indirect=True)
def test_source_identity_corrections_bind_hash_and_deterministic_reload(original_case):
    parent = load_domain_design(original_case[2])
    correction = _corrections(prefer_window_definitions=True)
    draft = retain_window_schema(parent, corrections=correction)
    for field, replacement in (("actor", "someone-else"), ("rationale", "different review"),
                               ("source_scoped_types", ["Model"])):
        values = draft.model_dump(mode="json")
        values["schema_projection"]["operator_corrections"][field] = replacement
        with pytest.raises(ValueError, match="CORRECTION_HASH_MISMATCH"):
            DomainDesignDraft.model_validate_json(canonical_json(values))
    for target in ("identity", "property", "relationship", "parent", "correction", "version"):
        values = draft.model_dump(mode="json")
        if target == "identity":
            values["sketch"]["types"][0]["identity_property_keys"] = ["record.variant"]
        elif target == "property":
            values["sketch"]["properties"][-1]["required"] = True
        elif target == "relationship":
            values["sketch"]["relationships"][0]["target_key"] = "record"
        elif target == "parent":
            values["schema_projection"]["parent_sketch"]["types"][0]["identity_property_keys"] = []
        elif target == "version":
            values["schema_projection"]["projection_version"] = "window-schema-projection/1.2.0"
        else:
            changed = values["schema_projection"]["operator_corrections"]
            changed["source_scoped_types"] = ["Model"]
            changed["correction_hash"] = canonical_sha256({
                key: value for key, value in changed.items() if key != "correction_hash"
            })
        with pytest.raises(ValueError, match="DERIVATION_MISMATCH|draft hash mismatch|IDENTITY_VERSION_MISMATCH"):
            DomainDesignDraft.model_validate_json(_reseal(values))
    with pytest.raises(ValueError, match="IDENTITY_VERSION_MISMATCH"):
        _derive(parent, correction, _projection_version="window-schema-projection/1.2.0")
    with pytest.raises(ValueError, match="frozen"):
        correction.source_scoped_types = ("Model",)


@pytest.mark.parametrize("original_case", [CompatibleSourceModel], indirect=True)
def test_historical_correction_serialization_and_all_projection_versions_are_unchanged(original_case):
    parent = load_domain_design(original_case[2])
    payload = {
        "correction_version": "window-design-corrections/1.0.0",
        "authority": "explicit_unapproved_schema_corrections",
        "actor": "historical-reviewer", "rationale": "Retain exact definitions",
        "route_targets": {}, "completeness": [], "prefer_window_definitions": True,
    }
    payload["correction_hash"] = canonical_sha256(payload)
    correction = WindowDesignCorrections.model_validate(payload)
    assert correction.model_dump(mode="json") == payload
    assert correction.source_scoped_types == ()
    for version in ("1.0.0", "1.1.0", "1.2.0"):
        sketch, projection = _derive(parent, correction, _projection_version="window-schema-projection/" + version)
        values = parent.model_dump(mode="json")
        values.update(artifact_version="8.0.0", model_call_count=0,
                      sketch=sketch.model_dump(mode="json"), schema_projection=projection.model_dump(mode="json"))
        historical = _reseal(values)
        loaded = DomainDesignDraft.model_validate_json(historical)
        assert canonical_json(loaded) == historical
        assert loaded.schema_projection.projection_version.endswith(version)
        assert "source_scoped_types" not in loaded.schema_projection.operator_corrections.model_dump(mode="json")
    assert retain_window_schema(parent).schema_projection.projection_version == "window-schema-projection/1.2.0"
