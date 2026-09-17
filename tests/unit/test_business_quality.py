"""Contract-driven quality and pre-write publication gates, entirely offline."""

from __future__ import annotations

import copy
import base64
import json

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.deploy import schema2_prototype as prototype
from fabric_kg_builder.serving.business_quality import (
    BusinessQualityError,
    RequirementPathRule,
    assess_business_quality,
    assess_business_quality_rows,
    assess_requirement_path_readiness,
    parse_quality_policy,
)
from fabric_kg_builder.serving.structured_publication import L5aPublicationError, compile_l5a_publication, run_l5a
from tests.unit.test_l5a_structured_publication import _FakeClient, _inputs


@pytest.fixture
def quality_inputs(tmp_path):
    inputs = _inputs(tmp_path / "sealed")
    source = inputs["source"]
    tables = {
        name: pq.read_table(source.resolve(name)).to_pylist()
        for name in ("semantic_publication_authority", "semantic_asserted_entities",
                     "semantic_asserted_properties", "semantic_asserted_relationships")
    }
    authority = tables["semantic_publication_authority"][0]
    contract = json.loads(authority["domain_contract_json"])
    policy = {
        "policy_version": "1.0.0",
        "domain_contract_hash": authority["domain_contract_hash"],
        "label_rules": {
            row["most_specific_type_id"]: {"allow_evidence_mention": True}
            for row in tables["semantic_asserted_entities"]
        },
    }
    row_args = {
        "contract": contract, "domain_contract_hash": authority["domain_contract_hash"],
        "tables": tables, "input_hashes": {"fixture": "f" * 64},
        "policy": policy, "crosswalks": inputs["crosswalks"],
    }
    l3 = tmp_path / "sealed" / ".fkg" / "l3"
    next(l3.rglob("output-manifest.json")).write_text(canonical_json(source.input_manifest) + "\n")
    return inputs, row_args, l3


def _codes(report):
    return report["summary"]["counts_by_code"]


def test_policy_module_import_does_not_require_cli_initialization():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c",
         "from fabric_kg_builder.serving.business_quality import parse_quality_policy"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_valid_grounded_rows_pass_and_policy_changes_fingerprint(quality_inputs):
    inputs, args, _ = quality_inputs
    report = assess_business_quality_rows(**args)
    assert report["summary"]["status"] == "pass"
    assert report["summary"]["blocker_count"] == 0
    assert report["semantic_precision"]["status"] == "not-assessed"
    assert all(p["missing_entities"] == 0 for p in report["property_coverage"])
    historical = compile_l5a_publication(**inputs)
    strict = compile_l5a_publication(**inputs, quality_policy=args["policy"])
    assert historical.fingerprint != strict.fingerprint
    assert historical.business_quality_report is None
    assert strict.business_quality_report["policy_hash"]
    assert all(d["business_quality"] == strict.business_quality_report for d in strict.definitions.values())


@pytest.mark.parametrize("value", ['""', '"   "', None])
def test_empty_required_values_block(quality_inputs, value):
    _, args, _ = quality_inputs
    args["tables"]["semantic_asserted_properties"][0]["normalized_value_json"] = value
    report = assess_business_quality_rows(**args)
    assert "REQUIRED_PROPERTY_MISSING" in _codes(report)
    assert report["summary"]["status"] == "blocked"
    finding = next(f for f in report["findings"] if f["code"] == "REQUIRED_PROPERTY_MISSING")
    assert finding["row_id"] and finding["property_id"] and finding["reason"]


@pytest.mark.parametrize("label", [None, "", "   "])
def test_empty_labels_block(quality_inputs, label):
    _, args, _ = quality_inputs
    args["tables"]["semantic_asserted_entities"][0]["label"] = label
    report = assess_business_quality_rows(**args)
    assert "PRESENTATION_LABEL_MISSING" in _codes(report)
    assert "GROUNDED_NAME_MISSING" in _codes(report)


def test_optional_unknowns_are_warnings_not_fabricated(quality_inputs):
    _, args, _ = quality_inputs
    prop = args["tables"]["semantic_asserted_properties"].pop()
    for entity_type in args["contract"]["candidate_model"]["entity_types"]:
        for definition in entity_type["declared_properties"]:
            if definition["property_id"] == prop["semantic_property_id"]:
                definition["required"] = False
    before = copy.deepcopy(args["tables"])
    report = assess_business_quality_rows(**args)
    assert report["summary"]["blocker_count"] == 0
    assert _codes(report)["OPTIONAL_PROPERTY_UNKNOWN"] == 1
    assert args["tables"] == before
    assert next(p for p in report["property_coverage"] if p["property_id"] == prop["semantic_property_id"])["null_fraction"] == 1


@pytest.mark.parametrize("value_type,value", [
    ("date", '"2026-01-01"'), ("datetime", '"2026-01-01T00:00:00Z"'),
    ("boolean", "false"), ("integer", "0"), ("number", "0.0"),
])
def test_meaningful_falsy_and_temporal_values_remain_populated(quality_inputs, value_type, value):
    _, args, _ = quality_inputs
    prop = args["tables"]["semantic_asserted_properties"][0]
    prop["value_type"], prop["normalized_value_json"] = value_type, value
    for entity_type in args["contract"]["candidate_model"]["entity_types"]:
        for definition in entity_type["declared_properties"]:
            if definition["property_id"] == prop["semantic_property_id"]:
                definition["value_type"] = value_type
    report = assess_business_quality_rows(**args)
    assert report["summary"]["blocker_count"] == 0


def test_source_value_and_missing_presentation_are_distinct(quality_inputs):
    _, args, _ = quality_inputs
    entity = args["tables"]["semantic_asserted_entities"][0]
    prop = next(p for p in args["tables"]["semantic_asserted_properties"] if p["entity_id"] == entity["entity_id"])
    args["policy"]["label_rules"][entity["most_specific_type_id"]] = {
        "property_ids": [prop["semantic_property_id"]], "allow_evidence_mention": False,
    }
    entity["label"] = None
    report = assess_business_quality_rows(**args)
    assert "PRESENTATION_LABEL_NAME_MISMATCH" in _codes(report)
    assert "REQUIRED_PROPERTY_MISSING" not in _codes(report)
    assert "GROUNDED_NAME_MISSING" not in _codes(report)
    args["tables"]["semantic_asserted_properties"].remove(prop)
    report = assess_business_quality_rows(**args)
    assert "GROUNDED_NAME_MISSING" in _codes(report)
    assert "REQUIRED_PROPERTY_MISSING" in _codes(report)


@pytest.mark.parametrize("label,value", [
    ("Model A", "Model A"),
    ("Model A", "Model\n A"),
    ("Caf\u00e9 model", "Cafe\u0301 model"),
])
def test_property_backed_name_accepts_independent_evidence_spans(quality_inputs, label, value):
    _, args, _ = quality_inputs
    entity = args["tables"]["semantic_asserted_entities"][0]
    prop = next(p for p in args["tables"]["semantic_asserted_properties"]
                if p["entity_id"] == entity["entity_id"])
    args["policy"]["label_rules"][entity["most_specific_type_id"]] = {
        "property_ids": [prop["semantic_property_id"]], "allow_evidence_mention": False,
    }
    entity["label"] = label
    prop["normalized_value_json"] = canonical_json(value)
    prop["evidence_span_ids"] = ["evidence:independent-field-span"]
    assert entity["label_evidence_span_id"] not in prop["evidence_span_ids"]
    assert assess_business_quality_rows(**args)["summary"]["blocker_count"] == 0

    entity["label"] = "Different model"
    assert "PRESENTATION_LABEL_NAME_MISMATCH" in _codes(assess_business_quality_rows(**args))
    entity["label"] = label
    entity["label_evidence_span_id"] = "evidence:not-linked-to-entity"
    assert "LABEL_EVIDENCE_LINK_MISSING" in _codes(assess_business_quality_rows(**args))
    prop["evidence_span_ids"] = []
    assert "GROUNDED_NAME_MISSING" in _codes(assess_business_quality_rows(**args))


def test_local_graph_mapping_gaps_do_not_claim_missing_source_evidence(quality_inputs):
    _, args, _ = quality_inputs
    mapping = args["crosswalks"][0].semantic_type_mappings[0]
    graph = {
        "parts": [{
            "path": name,
            "payload": base64.b64encode(canonical_json(value).encode()).decode(),
            "payloadType": "InlineBase64",
        } for name, value in {
            "dataSources.json": {"dataSources": [{
                "name": "source", "properties": {"path": f"Tables/dbo/{mapping.physical_table_id}"},
            }]},
            "graphDefinition.json": {"nodeTables": [{
                "dataSourceName": "source", "propertyMappings": [
                    {"sourceColumn": "__canonical_id", "propertyName": "id"},
                ],
            }]},
        }.items()],
    }
    report = assess_business_quality_rows(**args, graph_definition=graph)
    assert "GRAPH_LABEL_MAPPING_MISSING" in _codes(report)
    assert "GRAPH_PROPERTY_MAPPING_MISSING" in _codes(report)
    assert "EVIDENCE_LINK_MISSING" not in _codes(report)
    assert "REQUIRED_PROPERTY_MISSING" not in _codes(report)
    assert report["summary"]["blocker_count"] == 0
    assert report["input_hashes"]["graph_definition_hash"] == canonical_sha256(graph)


def test_missing_evidence_owner_and_mapping_reported_separately(quality_inputs):
    _, args, _ = quality_inputs
    prop = args["tables"]["semantic_asserted_properties"][0]
    prop["entity_id"] = "entity:absent"
    prop["evidence_span_ids"] = []
    args["crosswalks"] = [{
        "semantic_property_ownership_mappings": [],
        "semantic_type_mappings": [], "relationship_mappings": [],
    }]
    report = assess_business_quality_rows(**args)
    assert {"PROPERTY_OWNER_INVALID", "EVIDENCE_LINK_MISSING", "PROPERTY_MAPPING_MISSING", "RELATIONSHIP_MAPPING_MISSING"} <= set(_codes(report))


def test_explicit_endpoint_coverage_not_assumed_from_declared_type(quality_inputs):
    _, args, _ = quality_inputs
    relationship = args["tables"]["semantic_asserted_relationships"].pop()
    entity = next(e for e in args["tables"]["semantic_asserted_entities"] if e["entity_id"] == relationship["source_entity_id"])
    baseline = assess_business_quality_rows(**args)
    assert baseline["summary"]["blocker_count"] == 0
    assert "DECLARED_RELATIONSHIP_UNOBSERVED" in _codes(baseline)
    args["policy"]["endpoint_coverage"] = [{
        "relationship_type_id": relationship["semantic_relationship_id"],
        "semantic_type_id": entity["most_specific_type_id"], "endpoint": "source",
    }]
    report = assess_business_quality_rows(**args)
    assert _codes(report)["RELATIONSHIP_COVERAGE_MISSING"] == 1


def test_policy_hash_and_version_are_validated(quality_inputs):
    _, args, _ = quality_inputs
    args["policy"]["domain_contract_hash"] = "0" * 64
    with pytest.raises(ValueError, match="POLICY_CONTRACT_MISMATCH"):
        assess_business_quality_rows(**args)
    args["policy"]["policy_version"] = "999"
    with pytest.raises(ValueError):
        assess_business_quality_rows(**args)


def test_historical_reviewed_policy_preserves_canonical_hash():
    # Exact contents of the reviewed revision-nine quality-policy.json; no
    # dependence on an operator's session artifact paths in the test suite.
    names = {
        "procedure": ("title", False),
        "procedure_step": ("title", True),
        "tool": ("name", False),
        "consumable": ("name", False),
        "advisory": ("title", True),
        "device_model": ("name", False),
        "device_variant": ("name", True),
        "applicability_criterion": ("label", True),
        "device_component": ("name", False),
        "repair_item_requirement": ("label", True),
    }
    historical = {
        "policy_version": "1.0.0",
        "domain_contract_hash": "823998df268a8d6258a72e46da50122fb6b5d381141d33d14400933cfa92392e",
        "label_rules": {
            f"semantic-type:{name}": {
                "property_ids": [f"property:{name}.{prop}"],
                "allow_evidence_mention": allow,
            } for name, (prop, allow) in names.items()
        },
        "endpoint_coverage": [],
    }
    for name in ("service_part", "fastener"):
        historical["label_rules"][f"semantic-type:{name}"] = {
            "property_ids": ["property:service_part.name", "property:service_part.part_number"],
            "allow_evidence_mention": False,
        }
    expected_hash = "7381f3eff38ea1bc04cf1a72eb7984fcbdc50978397e78791d8ecfce82534441"
    assert canonical_sha256(historical) == expected_hash
    parsed = parse_quality_policy(historical)
    for dumped in (parsed.model_dump(), parsed.model_dump(mode="json"),
                   json.loads(parsed.model_dump_json())):
        assert dumped == historical
        assert canonical_sha256(dumped) == expected_hash
    historical["requirement_paths"] = []
    for rule in historical["label_rules"].values():
        rule["display_templates"] = []
        rule["derive_display_from_properties"] = False
    assert canonical_sha256(parse_quality_policy(historical).model_dump(mode="json")) == expected_hash
    historical["label_rules"]["semantic-type:service_part"]["display_templates"] = [
        "{property:service_part.part_number} {property:service_part.name}",
    ]
    assert canonical_sha256(parse_quality_policy(historical).model_dump(mode="json")) != expected_hash


@pytest.mark.parametrize("strict", [False, True])
def test_no_opt_in_keeps_legacy_report_shape_and_hash(quality_inputs, strict):
    _, args, _ = quality_inputs
    if not strict:
        args["policy"] = None
    original = assess_business_quality_rows(**args)
    assert "display_template_assessments" not in original["presentation_coverage"]
    assert "structural_requirement_path_readiness" not in original
    if strict:
        args["policy"]["requirement_paths"] = []
        for rule in args["policy"]["label_rules"].values():
            rule["display_templates"] = []
    assert assess_business_quality_rows(**args) == original
    assert original["report_hash"] == canonical_sha256({
        key: value for key, value in original.items() if key != "report_hash"
    })


@pytest.mark.parametrize("value", [None, {}, ""])
@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
def test_empty_feature_serialization_does_not_hide_invalid_copied_values(quality_inputs, value):
    _, args, _ = quality_inputs
    parsed = parse_quality_policy(args["policy"])
    with pytest.raises(ValueError):
        parse_quality_policy(parsed.model_copy(update={"requirement_paths": value}))
    type_id = next(iter(parsed.label_rules))
    parsed.label_rules[type_id] = parsed.label_rules[type_id].model_copy(update={"display_templates": value})
    with pytest.raises(ValueError):
        parse_quality_policy(parsed)


def test_publication_recomputes_before_any_client_operation(quality_inputs):
    inputs, args, _ = quality_inputs
    args["policy"]["label_rules"] = {}

    class NoRemote:
        def __getattr__(self, name):
            pytest.fail(f"Quality failure must precede client access: {name}")

    with pytest.raises(BusinessQualityError) as error:
        run_l5a(**inputs, quality_policy=args["policy"], client=NoRemote())
    assert "SEMANTIC_LABEL_RULE_UNSPECIFIED" in _codes(error.value.report)


def test_existing_quality_enabled_targets_cannot_be_downgraded(tmp_path, quality_inputs):
    inputs, args, _ = quality_inputs
    client = _FakeClient()
    result = run_l5a(
        **inputs, quality_policy=args["policy"], client=client,
        state_root=tmp_path / "strict",
    )
    assert result.receipt.status == "succeeded"
    client.calls.clear()
    with pytest.raises(L5aPublicationError, match="BUSINESS_QUALITY_DOWNGRADE"):
        run_l5a(**inputs, client=client, state_root=tmp_path / "without-policy")
    assert client.calls and all(verb == "inspect" for verb, _ in client.calls)


def _prototype_args(tmp_path, inputs, l3):
    return {
        "l4_run": inputs["source"].root, "l3_root": l3,
        "workspace_id": "11111111-1111-4111-8111-111111111111", "name_prefix": "quality",
        "dry_run": True, "approve_live": None, "plan_path": tmp_path / "plan.json",
        "materialize_dir": tmp_path / "materialized", "journal_path": tmp_path / "journal.json",
    }


def test_prototype_apply_cannot_drop_policy_or_trust_edited_report(tmp_path, monkeypatch, quality_inputs):
    inputs, args, l3 = quality_inputs
    options = _prototype_args(tmp_path, inputs, l3)
    plan = prototype.publish_schema2_prototype(**options, quality_policy=args["policy"])
    assert plan["business_quality"]["policy_hash"]
    assert plan["business_quality"]["input_hashes"]["l4_manifest_hash"] == inputs["source"].manifest.manifest_hash
    # An operator changes an assessment and even rehashes the whole plan.
    plan["business_quality"]["summary"]["grounded_label_count"] = 999
    plan["plan_hash"] = canonical_sha256({k: v for k, v in plan.items() if k != "plan_hash"})
    options["plan_path"].write_text(canonical_json(plan))
    options.update(dry_run=False, approve_live=plan["plan_hash"])
    monkeypatch.setattr(prototype, "_Run", lambda *a, **k: pytest.fail("No cloud journal/client before gate"))
    # Deliberately omit quality_policy: apply recovers and enforces the bound policy.
    with pytest.raises(prototype.PrototypePublicationError, match="quality assessment changed"):
        prototype.publish_schema2_prototype(**options)


@pytest.mark.parametrize("strict", [False, True])
def test_downstream_recompilation_preserves_bound_quality(tmp_path, quality_inputs, strict):
    inputs, args, l3 = quality_inputs
    options = _prototype_args(tmp_path, inputs, l3)
    plan = prototype.publish_schema2_prototype(
        **options, quality_policy=args["policy"] if strict else None,
    )
    compilation = prototype._compile_from_plan(inputs["source"].root, l3, plan)
    assert compilation.provenance == plan["provenance"]
    assert {name: prototype._table_proof(table) for name, table in compilation.tables.items()} == plan["tables"]
    if strict:
        assert compilation.definitions["ontology"]["business_quality"] == plan["business_quality"]
        plan["business_quality"]["summary"]["grounded_label_count"] = 999
        with pytest.raises(prototype.PrototypePublicationError, match="quality assessment changed"):
            prototype._compile_from_plan(inputs["source"].root, l3, plan)
        plan["business_quality"].pop("policy")
        with pytest.raises(prototype.PrototypePublicationError, match="quality policy is missing"):
            prototype._compile_from_plan(inputs["source"].root, l3, plan)


def test_prototype_policy_drift_and_empty_rules_fail_before_writes(tmp_path, monkeypatch, quality_inputs):
    inputs, args, l3 = quality_inputs
    options = _prototype_args(tmp_path, inputs, l3)
    plan = prototype.publish_schema2_prototype(**options, quality_policy=args["policy"])
    monkeypatch.setattr(prototype, "_Run", lambda *a, **k: pytest.fail("No cloud access"))
    policy = copy.deepcopy(args["policy"])
    policy["label_rules"] = {}
    with pytest.raises(prototype.PrototypePublicationError, match="quality policy changed"):
        prototype.publish_schema2_prototype(**{**options, "dry_run": False, "approve_live": plan["plan_hash"]}, quality_policy=policy)
    new_options = _prototype_args(tmp_path / "new", inputs, l3)
    with pytest.raises(BusinessQualityError):
        prototype.publish_schema2_prototype(**new_options, quality_policy=policy)


def test_local_cli_emits_machine_readable_report_and_blocker_exit(tmp_path, quality_inputs):
    from fabric_kg_builder.cli import cli

    inputs, args, l3 = quality_inputs
    policy = tmp_path / "policy.json"
    policy.write_text(canonical_json({**args["policy"], "label_rules": {}}))
    output = tmp_path / "assessment.json"
    result = CliRunner().invoke(cli, ["assess-business-quality",
        "--l4-run", str(inputs["source"].root), "--l3-root", str(l3),
        "--quality-policy", str(policy), "--output", str(output),
    ])
    assert result.exit_code == 5, result.output
    report = json.loads(output.read_text())
    assert report["summary"]["blocker_count"] == 2
    assert report["report_hash"] == canonical_sha256({k: v for k, v in report.items() if k != "report_hash"})
    baseline = assess_business_quality(inputs["source"], crosswalks=inputs["crosswalks"])
    assert baseline["summary"]["blocker_count"] == 0
    assert baseline["summary"]["warning_count"] == 2


def test_public_publish_cli_enforces_policy_without_cloud_calls(tmp_path, monkeypatch, quality_inputs):
    from fabric_kg_builder.cli import cli

    inputs, args, l3 = quality_inputs
    policy = tmp_path / "policy.json"
    policy.write_text(canonical_json({**args["policy"], "label_rules": {}}))
    monkeypatch.setattr(prototype, "_Run", lambda *a, **k: pytest.fail("No cloud access"))
    result = CliRunner().invoke(cli, [
        "app", "publish-structured", "--l4-run", str(inputs["source"].root),
        "--l3-root", str(l3), "--workspace-id", "11111111-1111-4111-8111-111111111111",
        "--name-prefix", "quality", "--plan", str(tmp_path / "plan.json"),
        "--materialize", str(tmp_path / "materialized"), "--prototype-create-only",
        "--prototype-journal", str(tmp_path / "journal.json"),
        "--quality-policy", str(policy), "--dry-run",
    ])
    assert result.exit_code == 1, result.output
    assert "BUSINESS_QUALITY_BLOCKED" in result.output
    assert not (tmp_path / "plan.json").exists()


def _template_input(args):
    entity = args["tables"]["semantic_asserted_entities"][0]
    type_id = entity["most_specific_type_id"]
    name = next(p for p in args["tables"]["semantic_asserted_properties"]
                if p["entity_id"] == entity["entity_id"])
    name_id, sku_id = name["semantic_property_id"], "property:test.part_number"
    name["normalized_value_json"] = canonical_json("Café model")
    sku = {**name, "property_assertion_id": "assertion:sku",
           "semantic_property_id": sku_id, "normalized_value_json": '"SKU-1"',
           "evidence_span_ids": ["evidence:independent-sku"]}
    args["tables"]["semantic_asserted_properties"].append(sku)
    definition = next(t for t in args["contract"]["candidate_model"]["entity_types"]
                      if t["type_id"] == type_id)
    definition["declared_properties"].append({
        "property_id": sku_id, "value_type": "string", "required": True,
    })
    args["contract"]["hierarchy_closure"]["effective_property_ids_by_type"][type_id].append(sku_id)
    args["crosswalks"] = None
    template = f"{{{sku_id}}} {{{name_id}}}"
    rule = {"property_ids": [name_id, sku_id], "display_templates": [template]}
    args["policy"]["label_rules"][type_id] = rule
    entity["label"] = "SKU-1 Cafe\u0301\nmodel"
    return entity, name, sku, rule


def test_reviewed_template_is_opt_in_grounded_auditable_and_hashed(quality_inputs):
    _, args, _ = quality_inputs
    entity, name, sku, rule = _template_input(args)
    template = rule.pop("display_templates")
    strict = assess_business_quality_rows(**args)
    assert "PRESENTATION_LABEL_NAME_MISMATCH" in _codes(strict)
    rule["display_templates"] = template
    before = copy.deepcopy(args)
    report = assess_business_quality_rows(**args)
    assert report["summary"]["blocker_count"] == 0
    assert args == before
    assert report["policy_hash"] != strict["policy_hash"]
    assert report["policy_hash"] == canonical_sha256(parse_quality_policy(args["policy"]).model_dump(mode="json"))
    audit = report["presentation_coverage"]["display_template_assessments"][0]
    assert audit["entity_id"] == entity["entity_id"]
    result = audit["templates"][0]
    assert result["status"] == "matched"
    assert result["rendered_label"] == "SKU-1 Café model"
    assert set(result["property_assertion_ids"]) == {name["property_assertion_id"], sku["property_assertion_id"]}
    assert "evidence:independent-sku" in result["evidence_span_ids"]
    assert report["semantic_precision"]["status"] == "not-assessed"
    entity["label"] = "Invented SKU-1 Café model"
    assert "PRESENTATION_LABEL_NAME_MISMATCH" in _codes(assess_business_quality_rows(**args))


def test_matched_template_still_requires_label_evidence(quality_inputs):
    _, args, _ = quality_inputs
    entity, _, _, _ = _template_input(args)
    entity["label_evidence_span_id"] = "not-linked"
    report = assess_business_quality_rows(**args)
    assert "LABEL_EVIDENCE_LINK_MISSING" in _codes(report)
    assert report["presentation_coverage"]["display_template_assessments"][0]["templates"][0]["status"] == "matched"
    assert report["summary"]["status"] == "blocked"


def test_templates_bind_publication_fingerprint_and_cli_policy_hash(tmp_path, quality_inputs):
    from fabric_kg_builder.cli import cli

    inputs, args, l3 = quality_inputs
    original = compile_l5a_publication(**inputs, quality_policy=args["policy"])
    entity = args["tables"]["semantic_asserted_entities"][0]
    prop = next(p for p in args["tables"]["semantic_asserted_properties"]
                if p["entity_id"] == entity["entity_id"])
    args["policy"]["label_rules"][entity["most_specific_type_id"]]["display_templates"] = [
        f"{{{prop['semantic_property_id']}}}",
    ]
    reviewed = compile_l5a_publication(**inputs, quality_policy=args["policy"])
    assert original.fingerprint != reviewed.fingerprint
    policy_path = tmp_path / "reviewed.json"
    policy_path.write_text(canonical_json(args["policy"]))
    output = tmp_path / "report.json"
    expected_hash = canonical_sha256(parse_quality_policy(args["policy"]).model_dump(mode="json"))
    result = CliRunner().invoke(cli, [
        "assess-business-quality", "--l4-run", str(inputs["source"].root),
        "--l3-root", str(l3), "--quality-policy", str(policy_path),
        "--expected-policy-hash", expected_hash, "--output", str(output),
    ])
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert report["policy_hash"] == expected_hash
    assert report["presentation_coverage"]["display_template_assessments"]
    assert "structural_requirement_path_readiness" not in report


@pytest.mark.parametrize("template,error", [
    ("literal only", "INVALID_DISPLAY_TEMPLATE"),
    ("{", "INVALID_DISPLAY_TEMPLATE"),
    ("{}", "INVALID_DISPLAY_TEMPLATE"),
    ("{{property:test.part_number}}", "INVALID_DISPLAY_TEMPLATE"),
    ("{missing}", "INVALID_TEMPLATE_PROPERTY"),
    ("{property:test.part_number!r}", "INVALID_TEMPLATE_PROPERTY"),
    ("{property:test.part_number:>10}", "INVALID_TEMPLATE_PROPERTY"),
    ("{property:test.part_number.__class__}", "INVALID_TEMPLATE_PROPERTY"),
])
def test_invalid_templates_fail_closed(quality_inputs, template, error):
    _, args, _ = quality_inputs
    _, _, _, rule = _template_input(args)
    rule["display_templates"] = [template]
    with pytest.raises(ValueError, match=error):
        assess_business_quality_rows(**args)


@pytest.mark.parametrize("value", [None, 'null', '""', '"  "', '[]', '{}', 'false', '1'])
def test_templates_never_interpolate_missing_or_nonstring_declared_string_values(quality_inputs, value):
    _, args, _ = quality_inputs
    _, _, sku, rule = _template_input(args)
    rule["property_ids"] = []
    sku["normalized_value_json"] = value
    report = assess_business_quality_rows(**args)
    assert "REQUIRED_PROPERTY_MISSING" in _codes(report)
    assert "GROUNDED_NAME_MISSING" in _codes(report)
    audit = report["presentation_coverage"]["display_template_assessments"][0]["templates"][0]
    assert audit["status"] == "unavailable"
    assert audit["rendered_label"] is None
    assert audit["unavailable_property_ids"] == [sku["semantic_property_id"]]


@pytest.mark.parametrize("failure", ["evidence", "blank-evidence", "conflict", "owner", "missing"])
def test_template_cannot_supply_ungrounded_or_ambiguous_facts(quality_inputs, failure):
    _, args, _ = quality_inputs
    _, _, sku, rule = _template_input(args)
    rule["property_ids"] = []
    if failure in ("evidence", "blank-evidence"):
        sku["evidence_span_ids"] = [] if failure == "evidence" else [" "]
    elif failure == "conflict":
        args["tables"]["semantic_asserted_properties"].append({
            **sku, "property_assertion_id": "conflicting:sku", "normalized_value_json": '"SKU-2"',
        })
    elif failure == "owner":
        sku["entity_id"] = "absent"
    else:
        args["tables"]["semantic_asserted_properties"].remove(sku)
    report = assess_business_quality_rows(**args)
    assert "GROUNDED_NAME_MISSING" in _codes(report)
    assert report["presentation_coverage"]["display_template_assessments"][0]["templates"][0]["status"] == "unavailable"


def test_template_uses_inherited_effective_properties_but_does_not_relax_required_facts(quality_inputs):
    _, args, _ = quality_inputs
    entity, name, sku, rule = _template_input(args)
    parent = entity["most_specific_type_id"]
    child = "semantic-type:child"
    args["contract"]["candidate_model"]["entity_types"].append({
        "type_id": child, "parent_type_id": parent, "declared_properties": [],
    })
    effective = args["contract"]["hierarchy_closure"]["effective_property_ids_by_type"]
    effective[child] = list(effective[parent])
    entity["most_specific_type_id"] = child
    args["policy"]["label_rules"][child] = rule
    assert assess_business_quality_rows(**args)["summary"]["blocker_count"] == 0
    rule["display_templates"] = [f"{{{name['semantic_property_id']}}}"]
    entity["label"] = "Café model"
    args["tables"]["semantic_asserted_properties"].remove(sku)
    report = assess_business_quality_rows(**args)
    assert "REQUIRED_PROPERTY_MISSING" in _codes(report)
    assert "GROUNDED_NAME_MISSING" not in _codes(report)
    effective[child].remove(name["semantic_property_id"])
    rule["property_ids"] = []
    with pytest.raises(ValueError, match="INVALID_TEMPLATE_PROPERTY"):
        assess_business_quality_rows(**args)


@pytest.mark.parametrize("value_type,value,rendered", [
    ("boolean", "false", "false"), ("integer", "0", "0"), ("number", "0.0", "0.0"),
    ("date", '"2026-09-13"', "2026-09-13"),
    ("datetime", '"2026-09-13T00:00:00Z"', "2026-09-13T00:00:00+00:00"),
])
def test_template_supports_grounded_declared_scalars(quality_inputs, value_type, value, rendered):
    _, args, _ = quality_inputs
    entity, _, sku, rule = _template_input(args)
    rule["property_ids"] = []
    sku["value_type"], sku["normalized_value_json"] = value_type, value
    for t in args["contract"]["candidate_model"]["entity_types"]:
        for p in t["declared_properties"]:
            if p["property_id"] == sku["semantic_property_id"]:
                p["value_type"] = value_type
    entity["label"] = f"{rendered} Café model"
    assert assess_business_quality_rows(**args)["summary"]["blocker_count"] == 0


@pytest.fixture
def requirement_path_inputs():
    procedure = "semantic-type:procedure"
    requirement = "semantic-type:repair_item_requirement"
    items = ["consumable", "fastener", "service_part", "tool"]
    owner_id = "relationship-type:procedure_has_requirement__procedure__repair_item_requirement"
    item_ids = [f"relationship-type:requirement_specifies_item__repair_item_requirement__{item}" for item in items]
    types = [procedure, requirement, *(f"semantic-type:{item}" for item in items)]
    contract = {
        "candidate_model": {
            "entity_types": [{"type_id": t, "declared_properties": []} for t in types],
            "relationship_types": [{
                "relationship_type_id": rel_id, "predicate_id": rel_id.replace("relationship-type:", "predicate:"),
                "source_type_ids": [source], "target_type_ids": [target], "endpoint_policy": "exact",
            } for rel_id, source, target in [
                (owner_id, procedure, requirement),
                *[(rel_id, requirement, f"semantic-type:{item}") for rel_id, item in zip(item_ids, items)],
            ]],
        },
        "hierarchy_closure": {"effective_property_ids_by_type": {t: [] for t in types}},
    }
    tables = {
        "semantic_asserted_entities": [{
            "entity_id": key, "most_specific_type_id": type_id, "label": key,
            "label_evidence_span_id": "evidence:1", "evidence_span_ids": ["evidence:1"],
        } for key, type_id in [("procedure:1", procedure), ("requirement:1", requirement),
                               ("item:1", "semantic-type:tool")]],
        "semantic_asserted_relationships": [{
            "relationship_id": key, "semantic_relationship_id": rel_id,
            "source_entity_id": source, "target_entity_id": target, "evidence_span_ids": ["evidence:1"],
        } for key, rel_id, source, target in [
            ("edge:owner", owner_id, "procedure:1", "requirement:1"),
            ("edge:item", item_ids[-1], "requirement:1", "item:1"),
        ]],
    }
    rule = RequirementPathRule(
        procedure_type_id=procedure, requirement_type_id=requirement,
        procedure_relationship_type_id=owner_id, item_relationship_type_ids=item_ids,
    )
    return {"contract": contract, "tables": tables, "rules": [rule]}


def test_requirement_path_readiness_is_separate_and_auditable(requirement_path_inputs):
    args = requirement_path_inputs
    report = assess_requirement_path_readiness(**args)
    path = report["paths"][0]
    assert path["status"] == "ready"
    assert path["ready_requirement_count"] == 1
    assert path["procedures_with_ready_paths"] == 1
    assert len(path["relationship_contracts"]) == 5
    assert all(row["predicate_id"].startswith("predicate:") for row in path["relationship_contracts"])
    assert report["question_accuracy"] == report["source_scope_completeness"] == "not-assessed"
    args["tables"]["semantic_asserted_relationships"].clear()
    policy = {
        "domain_contract_hash": "a" * 64,
        "label_rules": {e["most_specific_type_id"]: {"allow_evidence_mention": True}
                        for e in args["tables"]["semantic_asserted_entities"]},
        "requirement_paths": [args["rules"][0].model_dump(mode="json")],
    }
    combined = assess_business_quality_rows(
        contract=args["contract"], tables=args["tables"], input_hashes={},
        domain_contract_hash="a" * 64, policy=policy,
    )
    assert combined["summary"]["status"] == "pass"
    assert combined["structural_requirement_path_readiness"]["paths"][0]["status"] == "not-ready"
    assert combined["structural_requirement_path_readiness"]["gate_effect"] == "informational-only"
    assert "display_template_assessments" not in combined["presentation_coverage"]
    assert combined["policy"]["requirement_paths"] == policy["requirement_paths"]
    policy.pop("requirement_paths")
    without_paths = assess_business_quality_rows(
        contract=args["contract"], tables=args["tables"], input_hashes={},
        domain_contract_hash="a" * 64, policy=policy,
    )
    assert combined["policy_hash"] != without_paths["policy_hash"]
    assert combined["report_hash"] != without_paths["report_hash"]
    assert "structural_requirement_path_readiness" not in without_paths


@pytest.mark.parametrize("missing,reason", [("edge:owner", "PROCEDURE_LINK_MISSING"), ("edge:item", "ITEM_LINK_MISSING")])
def test_requirement_paths_missing_half_is_not_ready(requirement_path_inputs, missing, reason):
    args = requirement_path_inputs
    args["tables"]["semantic_asserted_relationships"] = [
        e for e in args["tables"]["semantic_asserted_relationships"] if e["relationship_id"] != missing
    ]
    path = assess_requirement_path_readiness(**args)["paths"][0]
    assert path["ready_requirement_count"] == 0
    assert path["requirements"][0]["reasons"] == [reason]
    assert path["procedure_ids_without_ready_paths"] == ["procedure:1"]


@pytest.mark.parametrize("failure", ["absent-endpoint", "wrong-type", "edge-evidence", "entity-evidence", "reversed"])
def test_requirement_paths_reject_invalid_edges(requirement_path_inputs, failure):
    args = requirement_path_inputs
    edge = args["tables"]["semantic_asserted_relationships"][1]
    item = args["tables"]["semantic_asserted_entities"][2]
    if failure == "absent-endpoint":
        edge["target_entity_id"] = "missing"
    elif failure == "wrong-type":
        item["most_specific_type_id"] = "semantic-type:procedure"
    elif failure == "edge-evidence":
        edge["evidence_span_ids"] = []
    elif failure == "entity-evidence":
        item["evidence_span_ids"] = []
    else:
        edge["source_entity_id"], edge["target_entity_id"] = edge["target_entity_id"], edge["source_entity_id"]
    path = assess_requirement_path_readiness(**args)["paths"][0]
    assert path["status"] == "not-ready"
    assert path["invalid_relationship_ids"] == ["edge:item"]
    assert path["ready_requirement_count"] == 0


def test_requirement_items_are_union_alternatives_not_four_mandatory_edges(requirement_path_inputs):
    args = requirement_path_inputs
    edge = args["tables"]["semantic_asserted_relationships"][1]
    args["tables"]["semantic_asserted_relationships"].append({**edge, "relationship_id": "duplicate:same-item"})
    assert assess_requirement_path_readiness(**args)["paths"][0]["ready_requirement_count"] == 1
    args["tables"]["semantic_asserted_entities"].append({
        **args["tables"]["semantic_asserted_entities"][2],
        "entity_id": "item:2", "most_specific_type_id": "semantic-type:consumable",
    })
    args["tables"]["semantic_asserted_relationships"].append({
        **edge, "relationship_id": "different:item", "target_entity_id": "item:2",
        "semantic_relationship_id": args["rules"][0].item_relationship_type_ids[0],
    })
    path = assess_requirement_path_readiness(**args)["paths"][0]
    assert path["ready_requirement_count"] == 0
    assert path["requirements"][0]["reasons"] == ["MULTIPLE_DISTINCT_ITEMS"]


def test_requirement_paths_empty_population_and_invalid_policy(requirement_path_inputs):
    args = requirement_path_inputs
    args["tables"]["semantic_asserted_entities"] = []
    args["tables"]["semantic_asserted_relationships"] = []
    assert assess_requirement_path_readiness(**args)["paths"][0]["status"] == "unobserved"
    rule = args["rules"][0].model_dump(mode="json")
    rule["item_relationship_type_ids"] = ["relationship-type:unknown"]
    args["rules"] = [rule]
    with pytest.raises(ValueError, match="INVALID_REQUIREMENT_PATH_RULE"):
        assess_requirement_path_readiness(**args)
    rule["item_relationship_type_ids"] = [rule["procedure_relationship_type_id"]]
    with pytest.raises(ValueError, match="INCOMPATIBLE_REQUIREMENT_PATH_RULE"):
        assess_requirement_path_readiness(**args)


def test_requirement_paths_honor_endpoint_subtype_policy(requirement_path_inputs):
    args = requirement_path_inputs
    types = args["contract"]["candidate_model"]["entity_types"]
    types.append({
        "type_id": "semantic-type:special_tool", "parent_type_id": "semantic-type:tool",
        "declared_properties": [],
    })
    args["tables"]["semantic_asserted_entities"][2]["most_specific_type_id"] = "semantic-type:special_tool"
    assert assess_requirement_path_readiness(**args)["paths"][0]["status"] == "not-ready"
    args["contract"]["candidate_model"]["relationship_types"][-1]["endpoint_policy"] = "allow_subtypes"
    assert assess_requirement_path_readiness(**args)["paths"][0]["status"] == "ready"


def _derived_args(raw, *, sku=None, name=None, tool=False):
    type_id = "semantic-type:tool" if tool else "semantic-type:service_part"
    name_id = "property:tool.name" if tool else "property:service_part.name"
    sku_id = "property:service_part.part_number"
    definitions = [{"property_id": name_id, "value_type": "string", "required": tool}]
    if not tool:
        definitions.append({"property_id": sku_id, "value_type": "string", "required": True})
    entity = {
        "entity_id": "entity:owner", "most_specific_type_id": type_id, "label": raw,
        "evidence_span_ids": ["evidence:mention"], "label_evidence_span_id": "evidence:mention",
    }
    properties = [{
        "property_assertion_id": "assertion:" + key, "entity_id": "entity:owner",
        "semantic_property_id": key, "value_type": "string",
        "normalized_value_json": canonical_json(value), "evidence_span_ids": ["evidence:" + key],
    } for key, value in [(name_id, name), (sku_id, sku)] if value is not None]
    return {
        "contract": {
            "candidate_model": {"entity_types": [{
                "type_id": type_id, "declared_properties": definitions,
            }], "relationship_types": []},
            "hierarchy_closure": {"effective_property_ids_by_type": {
                type_id: [p["property_id"] for p in definitions],
            }},
        },
        "domain_contract_hash": "a" * 64, "input_hashes": {},
        "tables": {"semantic_asserted_entities": [entity], "semantic_asserted_properties": properties},
        "policy": {
            "domain_contract_hash": "a" * 64,
            "label_rules": {type_id: {
                "property_ids": [name_id] if tool else [name_id, sku_id],
                "display_templates": [] if tool else [f"{{{sku_id}}} {{{name_id}}}"],
                "derive_display_from_properties": True,
            }},
        },
    }


@pytest.mark.parametrize("raw,sku,name,tool,expected", [
    ("M1167779 Screws", "M1167779", None, False, "M1167779"),
    ("M1288974 Foam x 1 (T3 Shield Foam #2)", "M1288974", "Foam", False, "M1288974 Foam"),
    ("M1288974 Foam x1(T3 Shield Foam #2)", "M1288974", "Foam", False, "M1288974 Foam"),
    ("Anti-Static wrist strap", None, "Anti-Static wrist strap (1M Ohm resistance)", True,
     "Anti-Static wrist strap (1M Ohm resistance)"),
    ("Foam", "M1288974", "Foam", False, "M1288974 Foam"),
])
def test_derived_instance_displays_are_grounded_not_new_facts(raw, sku, name, tool, expected):
    args = _derived_args(raw, sku=sku, name=name, tool=tool)
    before = copy.deepcopy(args)
    report = assess_business_quality_rows(**args)
    assert report["summary"]["status"] == "pass"
    display = report["presentation_coverage"]["derived_instance_displays"][0]
    assert display["original_mention"] == raw
    assert display["derived_display_label"] == expected
    assert display["status"] == "derived"
    assert display["property_assertion_ids"] == sorted(
        p["property_assertion_id"] for p in args["tables"]["semantic_asserted_properties"]
    )
    assert display["evidence_span_ids"]
    assert args == before
    assert report["semantic_precision"]["status"] == "not-assessed"
    if name is None:
        assert len(args["tables"]["semantic_asserted_properties"]) == 1
        assert "OPTIONAL_PROPERTY_UNKNOWN" in _codes(report)
    rule = next(iter(args["policy"]["label_rules"].values()))
    rule.pop("derive_display_from_properties")
    strict = assess_business_quality_rows(**args)
    assert "derived_instance_displays" not in strict["presentation_coverage"]
    assert strict["policy_hash"] != report["policy_hash"]
    if raw != "Foam":
        assert "PRESENTATION_LABEL_NAME_MISMATCH" in _codes(strict)


@pytest.mark.parametrize("raw,sku,name,tool", [
    ("M123 SSD", "M123", "RAM", False),
    ("1", None, "Tool", True),
    ("M12", "M123", None, False),
    ("M1234 Screws", "M123", None, False),
    ("M123.4 Screws", "M123", None, False),
    ("M123 Foaming", "M123", "Foam", False),
    ("Foam incompatible with M123", "M123", "Foam", False),
    ("Anti-Static ankle strap", None, "Anti-Static wrist strap (1M Ohm resistance)", True),
])
def test_derived_instance_contradictions_never_pass(raw, sku, name, tool):
    args = _derived_args(raw, sku=sku, name=name, tool=tool)
    # The flag must not become an evidence-mention mismatch waiver.
    next(iter(args["policy"]["label_rules"].values()))["allow_evidence_mention"] = True
    report = assess_business_quality_rows(**args)
    assert report["summary"]["status"] == "blocked"
    assert "PRESENTATION_LABEL_NAME_MISMATCH" in _codes(report)
    assert report["presentation_coverage"]["derived_instance_displays"][0]["derived_display_label"] is None


@pytest.mark.parametrize("failure", ["ambiguous", "property-evidence", "mention-evidence", "foreign-owner", "missing-name"])
def test_derived_display_preserves_owner_evidence_and_name_gates(failure):
    args = _derived_args("Tool", name="Tool (qualified)", tool=True)
    props = args["tables"]["semantic_asserted_properties"]
    if failure == "ambiguous":
        props.append({**props[0], "property_assertion_id": "conflict", "normalized_value_json": '"Other tool"'})
    elif failure == "property-evidence":
        props[0]["evidence_span_ids"] = []
    elif failure == "mention-evidence":
        args["tables"]["semantic_asserted_entities"][0]["label_evidence_span_id"] = "missing"
    elif failure == "foreign-owner":
        props[0]["entity_id"] = "entity:different-owner"
    else:
        props.clear()
    report = assess_business_quality_rows(**args)
    assert report["summary"]["status"] == "blocked"
    assert report["presentation_coverage"]["derived_instance_displays"][0]["derived_display_label"] is None
    if failure in ("foreign-owner", "missing-name"):
        assert "REQUIRED_PROPERTY_MISSING" in _codes(report)
        assert "GROUNDED_NAME_MISSING" in _codes(report)


def test_sku_only_policy_cannot_ignore_asserted_name_contradiction():
    args = _derived_args("M123 SSD", sku="M123", name="RAM")
    rule = next(iter(args["policy"]["label_rules"].values()))
    rule["property_ids"] = ["property:service_part.part_number"]
    rule["display_templates"] = []
    assert assess_business_quality_rows(**args)["summary"]["status"] == "blocked"


def test_derived_name_does_not_invent_required_sku():
    args = _derived_args("Screw", name="Screw")
    report = assess_business_quality_rows(**args)
    assert "REQUIRED_PROPERTY_MISSING" in _codes(report)
    assert report["summary"]["status"] == "blocked"
    assert report["presentation_coverage"]["derived_instance_displays"][0]["derived_display_label"] == "Screw"


def test_derived_flag_false_retains_legacy_hash_and_report(quality_inputs):
    _, args, _ = quality_inputs
    before = assess_business_quality_rows(**args)
    for rule in args["policy"]["label_rules"].values():
        rule["derive_display_from_properties"] = False
    assert assess_business_quality_rows(**args) == before


def _derived_sealed_inputs(tmp_path, monkeypatch):
    from tests.unit import test_schema2_projection_stage as projection_fixtures
    from tests.unit import test_schema2_validation_stage as fixtures

    monkeypatch.setattr(fixtures, "_SENTENCE", fixtures._SENTENCE + " A governed record (qualified).")
    original_pipeline = projection_fixtures._pipeline

    def pipeline(*a, mutate=None, **kw):
        def with_qualified_name(candidates, work_unit):
            candidates = mutate(candidates, work_unit)
            for candidate in candidates:
                if candidate["candidate_kind"] == "entity" and candidate["local_id"] == "record-1":
                    candidate["label"] = "governed record"
                    candidate["anchors"] = [{
                        "span_start": work_unit.slice_start,
                        "span_end": work_unit.slice_start + len(work_unit.text.rstrip()),
                        "quote": work_unit.text.rstrip(), "model_authored_evidence_id": None,
                    }]
                if candidate["candidate_kind"] == "property" and candidate["owner_local_id"] == "record-1":
                    candidate["value"] = candidate["normalized_value"] = "governed record (qualified)"
            return candidates
        return original_pipeline(*a, mutate=with_qualified_name, **kw)

    monkeypatch.setattr(projection_fixtures, "_pipeline", pipeline)
    inputs = _inputs(tmp_path / "sealed")
    source = inputs["source"]
    l3 = tmp_path / "sealed" / ".fkg" / "l3"
    next(l3.rglob("output-manifest.json")).write_text(canonical_json(source.input_manifest) + "\n")
    authority = pq.read_table(source.resolve("semantic_publication_authority")).to_pylist()[0]
    entities = pq.read_table(source.resolve("semantic_asserted_entities")).to_pylist()
    policy = {
        "domain_contract_hash": authority["domain_contract_hash"],
        "label_rules": {e["most_specific_type_id"]: {"allow_evidence_mention": True} for e in entities},
    }
    policy["label_rules"]["semantic-type:manufacturing.record"] = {
        "property_ids": ["property:record:canonical-id"], "derive_display_from_properties": True,
    }
    return inputs, policy, l3


def test_governed_publication_cli_binds_derived_label_policy(tmp_path, monkeypatch):
    from fabric_kg_builder.cli.main import cli

    inputs, policy, l3 = _derived_sealed_inputs(tmp_path, monkeypatch)
    policy_path = tmp_path / "quality-policy.json"
    policy_path.write_text(canonical_json(policy))
    plan_path = tmp_path / "publication-plan.json"
    result = CliRunner().invoke(cli, [
        "app", "publish-structured",
        "--l4-run", str(inputs["source"].root), "--l3-root", str(l3),
        "--workspace-id", "11111111-1111-4111-8111-111111111111",
        "--name-prefix", "quality", "--quality-policy", str(policy_path),
        "--plan", str(plan_path), "--dry-run",
    ])
    assert result.exit_code == 0, (result.output, result.exception)
    plan = json.loads(plan_path.read_text())
    assert plan["business_quality"]["presentation_coverage"]["derived_instance_displays"]
    assert plan["definition_hashes"]


def test_derived_display_flows_through_compiled_frames_native_bindings_and_hashes(tmp_path, monkeypatch):
    import hashlib
    from fabric_kg_builder.serving.business_quality import DERIVED_ENTITY_TABLE

    inputs, policy, l3 = _derived_sealed_inputs(tmp_path, monkeypatch)
    workspace, lakehouse = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    source = inputs["source"]
    files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.root.rglob("*") if p.is_file()}
    original = prototype._compile(source.root, l3, workspace, "quality")
    compiled = prototype._compile(source.root, l3, workspace, "quality", quality_policy=policy)
    raw = compiled.tables["l4_semantic_asserted_entities"].to_pylist()
    entity = next(e for e in raw if e["most_specific_type_id"] == "semantic-type:manufacturing.record")
    assert entity["label"] == "governed record"
    node = next(n for n in compiled.definitions["ontology"]["entity_types"]
                if n["canonical_semantic_type_id"] == entity["most_specific_type_id"])
    table_id = node["physical_table_id"]
    assert compiled.tables[table_id]["__label"].to_pylist() == ["governed record (qualified)"]
    assert prototype._table_proof(compiled.tables[table_id]) != prototype._table_proof(original.tables[table_id])
    for name in original.tables:
        if name.startswith("l4_"):
            assert compiled.tables[name].equals(original.tables[name])
    assert next(r for r in compiled.tables[DERIVED_ENTITY_TABLE].to_pylist()
                if r["entity_id"] == entity["entity_id"]) == {
        "entity_id": entity["entity_id"], "label": "governed record (qualified)", "original_mention": "governed record",
    }
    parts = prototype._ontology_parts(compiled, workspace, lakehouse, "quality", "test")
    payloads = {p["path"]: json.loads(base64.b64decode(p["payload"])) for p in parts}
    definition = payloads[f"EntityTypes/{node['id']}/definition.json"]
    display_id = definition["displayNamePropertyId"]
    binding = next(value for path, value in payloads.items()
                   if path.startswith(f"EntityTypes/{node['id']}/DataBindings/"))
    assert display_id != definition["entityIdParts"][0]
    assert "__label" in canonical_json(binding)
    assert table_id in canonical_json(binding)
    legacy = prototype._definition_payloads({"parts": prototype._ontology_parts(
        compiled, workspace, lakehouse, "quality", "test", legacy_names=True,
    )})
    assert legacy[f"EntityTypes/{node['id']}/definition.json"]["displayNamePropertyId"] == display_id
    graph = next(p["payload_json"] for p in compiled.graph_parts if p["path"] == "graphDefinition.json")
    assert any(m["sourceColumn"] == "__label" for n in graph["nodeTables"] for m in n["propertyMappings"])
    from fabric_kg_builder.serving.graph_model import encode_parts_for_api
    from tests.unit.test_schema2_companion_verification import _native

    companion = _native(compiled, {"parts": parts}, workspace=workspace, lakehouse=lakehouse)
    for definition, is_companion in [
        ({"parts": encode_parts_for_api(compiled.graph_parts)}, False), (companion, True),
    ]:
        checks = prototype._graph_readback_checks(
            definition, compiled, workspace,
            lakehouse if is_companion else prototype.LAKEHOUSE_REFERENCE,
            companion=is_companion, native_ontology={"parts": parts} if is_companion else None,
        )
        label_checks = [check for check in checks if any(
            "governed record (qualified)" in row.values() for row in check.rows
        )]
        assert label_checks
        for check in label_checks:
            raw_rows = [
                {key: "governed record" if value == "governed record (qualified)" else value
                 for key, value in row.items()} for row in check.rows
            ]
            assert prototype._graph_row_fingerprint(raw_rows, check.fields, from_wire=True) != (
                prototype._graph_row_fingerprint(check.rows, check.fields, from_wire=False)
            )
    from fabric_kg_builder.deploy import schema2_prototype_query as query_module
    from types import SimpleNamespace

    schema = query_module._schema({"parts": encode_parts_for_api(compiled.graph_parts)}, compiled)
    label, query_node = next((label, item) for label, item in schema["nodes"].items()
                            if item["table_id"] == table_id)
    prop = next(key for key, column in query_node["properties"].items() if column == "__label")
    query = SimpleNamespace(
        nodes={"n": label}, edges={}, identity_columns={"n": "entity"}, schema=schema,
        outputs={"display": ("n", prop)},
    )
    citations = query_module._citations(
        query, {"display": "governed record (qualified)", "entity": entity["entity_id"]},
        compiled, query_module._evidence(source, l3), query_module._citation_index(compiled, schema),
    )
    display_citations = [c for c in citations if c["answer_columns"] == ["display"]]
    assert display_citations
    assert all(c["claim_kind"] == "derived-instance-display" for c in display_citations)
    assert {c["assertion_id"] for c in display_citations} == {
        p["property_assertion_id"] for p in compiled.tables["l4_semantic_asserted_properties"].to_pylist()
        if p["entity_id"] == entity["entity_id"]
    }
    prototype._materialize(compiled, tmp_path / "materialized")
    assert pq.read_table(tmp_path / "materialized" / "tables" / f"{table_id}.parquet").equals(compiled.tables[table_id])
    options = _prototype_args(tmp_path / "plan", inputs, l3)
    plan = prototype.publish_schema2_prototype(**options, quality_policy=policy)
    rebuilt = prototype._compile_from_plan(source.root, l3, plan)
    assert rebuilt.tables[table_id].equals(compiled.tables[table_id])
    assert plan["tables"][table_id] == prototype._table_proof(rebuilt.tables[table_id])
    assert plan["business_quality"]["presentation_coverage"]["derived_instance_displays"]
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in files.items())


def test_derived_frame_hashes_and_final_report_bind_governed_assets(tmp_path, monkeypatch):
    from fabric_kg_builder.serving.structured_publication import build_l5a_governed_assets

    inputs, policy, _ = _derived_sealed_inputs(tmp_path, monkeypatch)
    with pytest.raises(L5aPublicationError, match="GOVERNED_ASSET_AUTHORITY_MISMATCH"):
        compile_l5a_publication(**inputs, quality_policy=policy)
    by_id = {asset.asset_id: asset for asset in inputs["governed_assets"]}
    assets = build_l5a_governed_assets(
        inputs["source"], crosswalks=inputs["crosswalks"], access_policy=inputs["access_policy"],
        target_ids=inputs["target_ids"], quality_policy=policy,
        storage_references={kind: by_id[target].storage_reference for kind, target in inputs["target_ids"].items()},
        immutable_locators={kind: by_id[target].immutable_locator for kind, target in inputs["target_ids"].items()},
    )
    inputs["governed_assets"] = assets
    compiled = compile_l5a_publication(**inputs, quality_policy=policy)
    by_id = {asset.asset_id: asset for asset in assets}
    for kind, definition in compiled.definitions.items():
        assert by_id[inputs["target_ids"][kind]].content_hash == canonical_sha256(definition)
    result = run_l5a(**inputs, quality_policy=policy, client=_FakeClient(), state_root=tmp_path / "publication")
    assert result.receipt.status == "succeeded"


def test_derived_widened_companion_binds_presentation_not_asserted_entity_table(tmp_path, monkeypatch):
    from fabric_kg_builder.serving.business_quality import DERIVED_ENTITY_TABLE
    from tests.unit.test_schema2_companion_verification import _native

    inputs, policy, l3 = _derived_sealed_inputs(tmp_path, monkeypatch)
    workspace, lakehouse = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    compiled = prototype._compile(inputs["source"].root, l3, workspace, "quality", quality_policy=policy)
    # Exercise native endpoint widening over real compiled frames, without
    # changing the sealed fixture's domain or publishing the synthetic binding.
    ontology = compiled.definitions["ontology"]
    edge = ontology["relationship_types"][0]
    edge["allowed_target_semantic_type_ids"] = sorted({
        *edge["allowed_target_semantic_type_ids"], *edge["allowed_source_semantic_type_ids"],
    })
    parts = prototype._ontology_parts(compiled, workspace, lakehouse, "quality", "test")
    payloads = prototype._definition_payloads({"parts": parts})
    assert DERIVED_ENTITY_TABLE in canonical_json(payloads)
    assert "l4_semantic_asserted_entities" not in canonical_json(payloads)
    companion = _native(compiled, {"parts": parts}, workspace=workspace, lakehouse=lakehouse)
    checks = prototype._graph_readback_checks(
        companion, compiled, workspace, lakehouse, companion=True, native_ontology={"parts": parts},
    )
    assert checks
    assert any("governed record (qualified)" in row.values() for c in checks for row in c.rows)
    assert "governed record" in compiled.tables["l4_semantic_asserted_entities"]["label"].to_pylist()


def test_derived_display_reaches_every_typed_membership():
    from types import SimpleNamespace
    import pyarrow as pa
    from fabric_kg_builder.serving.structured_publication import _entity_tables

    source = {
        "semantic_asserted_entities": pa.Table.from_pylist([{
            "entity_id": "entity:part", "label": "M1167779 Screws",
            "most_specific_type_id": "semantic-type:fastener",
        }]),
        "semantic_entity_type_assertions": pa.Table.from_pylist([{
            "entity_id": "entity:part", "semantic_type_id": type_id, "hierarchy_depth": depth,
        } for depth, type_id in enumerate(["semantic-type:service_part", "semantic-type:fastener"])]),
    }
    before = {name: table.to_pylist() for name, table in source.items()}
    crosswalk = SimpleNamespace(semantic_type_mappings=[
        SimpleNamespace(canonical_semantic_type_id=type_id, physical_table_id=table_id,
                        physical_property_bindings=[])
        for type_id, table_id in [
            ("semantic-type:service_part", "parts"), ("semantic-type:fastener", "fasteners"),
        ]
    ])
    typed = _entity_tables(
        source, crosswalk, {}, frozenset(), derived_display_labels={"entity:part": "M1167779"},
    )
    assert all(table["__label"].to_pylist() == ["M1167779"] for table in typed.values())
    assert all(table["__canonical_id"].to_pylist() == ["entity:part"] for table in typed.values())
    assert {name: table.to_pylist() for name, table in source.items()} == before
