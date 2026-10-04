"""Exact external Copilot authority, independent of publication label allocation."""

import base64
import copy
import json

import pytest

from fabric_kg_builder.deploy.naming_review import load_review, render_ontology
from fabric_kg_builder.deploy.fabric_ontology_definition import compile_fabric_ontology_definition


def write_review(path, context, suffix=""):
    domain = context["evidence"]["approved_domain_contract"]
    model = domain["candidate_model"]
    payload = {
        "version": "copilot-semantic-names/1.0.0",
        "author": "Copilot semantic review (synthetic test fixture)",
        "domain_contract_hash": context["evidence"]["publication_authority"]["domain_contract_hash"],
        "entity_types": {
            item["type_id"]: {
                "native_name": f"BusinessType{i}{suffix}", "display_name": f"Business type {i}",
                "reason": "Synthetic reviewer-approved business noun.",
            }
            for i, item in enumerate(model["entity_types"])
        },
        "relationship_types": {
            item["relationship_type_id"]: {
                "native_name": f"HasBusinessRole{i}{suffix}", "display_name": f"Has business role {i}",
                "verb": "Has", "source_type_ids": item["source_type_ids"],
                "target_type_ids": item["target_type_ids"],
                "reason": "Synthetic reviewer-approved directed relationship.",
            }
            for i, item in enumerate(model["relationship_types"])
        },
    }
    path.write_text(json.dumps(payload))
    return payload


@pytest.fixture
def reviewed(tmp_path):
    entities = [{"type_id": f"type:{i}", "display_name": "Duplicate"} for i in range(12)]
    relationships = [{
        "relationship_type_id": f"rel:{i}", "display_name": "Duplicate",
        "source_type_ids": [f"type:{i % 12}"], "target_type_ids": [f"type:{(i // 12 + 1) % 12}"],
    } for i in range(38)]
    domain = {"candidate_model": {"entity_types": entities, "relationship_types": relationships}}
    ontology = {
        "entity_types": [{
            "id": str(1000001 + i), "canonical_semantic_type_id": item["type_id"],
            "physical_table_id": f"table_{i}", "physical_identity_column": "__canonical_id", "properties": [],
        } for i, item in enumerate(entities)],
        "relationship_types": [{
            "id": str(3000001 + i), "canonical_semantic_relationship_id": item["relationship_type_id"],
            "physical_table_id": f"edge_{i}", "allowed_source_semantic_type_ids": item["source_type_ids"],
            "allowed_target_semantic_type_ids": item["target_type_ids"],
            "source_identity_column": "__source_entity_id", "target_identity_column": "__target_entity_id",
        } for i, item in enumerate(relationships)],
    }
    context = {"evidence": {
        "approved_domain_contract": domain, "publication_authority": {"domain_contract_hash": "a" * 64},
    }}
    path = tmp_path / "review.json"
    payload = write_review(path, context)
    return path, payload, domain, ontology


def test_all_12_types_38_pairs_exact_native_names_without_fallback(reviewed):
    path, payload, domain, ontology = reviewed
    raw = path.read_bytes()
    review, evidence = load_review(path, domain, "a" * 64, ontology)
    parts = compile_fabric_ontology_definition(
        ontology, workspace_id="workspace", lakehouse_id="lakehouse", lakehouse="dbo",
        display_name="Test", description="Synthetic", legacy_names=True,
    ).parts
    before = {"parts": list(parts)}
    after, mapping = render_ontology(before, ontology, review)
    assert len(mapping) == 50
    assert base64.b64decode(evidence["raw_base64"]) == raw
    assert evidence["payload"] == payload
    assert len(evidence["code_sha256"]) == 64
    for left, right in zip(before["parts"], after["parts"], strict=True):
        old, new = json.loads(base64.b64decode(left["payload"])), json.loads(base64.b64decode(right["payload"]))
        if left == right:
            continue
        expected = next(item for item in mapping if item["id"] == str(new["id"]))
        assert new["name"] == expected["new_name"]
        new["name"] = old["name"]
        assert old == new
    assert all(item["new_name"].startswith("Has") for item in mapping if item["kind"] == "relationship_type")
    assert domain["candidate_model"]["entity_types"][0]["display_name"] == "Duplicate"


@pytest.mark.parametrize("mutation", [
    lambda p: p["entity_types"].pop("type:0"),
    lambda p: p["entity_types"].update({"extra": p["entity_types"]["type:0"]}),
    lambda p: p["relationship_types"].pop("rel:0"),
    lambda p: p["relationship_types"].update({"extra": p["relationship_types"]["rel:0"]}),
    lambda p: p.update(domain_contract_hash="b" * 64),
    lambda p: p["entity_types"]["type:0"].update(native_name="BusinessType1"),
    lambda p: p["entity_types"]["type:0"].update(native_name="Type-with-hyphen"),
    lambda p: p["relationship_types"]["rel:0"].update(native_name="GraphHasItem"),
    lambda p: p["relationship_types"]["rel:0"].update(verb="Invents"),
    lambda p: p["relationship_types"]["rel:0"].update(native_name="HasItem_deadbeef"),
    lambda p: p["relationship_types"]["rel:0"].update(source_type_ids=["type:11"]),
    lambda p: p["relationship_types"]["rel:0"].update(target_type_ids=["type:11"]),
    lambda p: p["relationship_types"]["rel:0"].update(reason=" "),
    lambda p: p.update(properties={}),
])
def test_review_invalid_authority_fails_closed(reviewed, mutation):
    path, payload, domain, ontology = reviewed
    mutation(payload)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_review(path, domain, "a" * 64, ontology)


def test_duplicate_json_keys_rejected_and_business_digits_preserved(reviewed):
    path, payload, domain, ontology = reviewed
    payload["entity_types"]["type:0"]["native_name"] = "M365Connector"
    path.write_text(json.dumps(payload))
    assert load_review(path, domain, "a" * 64, ontology)[0].entity_types["type:0"].native_name == "M365Connector"
    path.write_text('{"version": "one", "version": "two"}')
    with pytest.raises(ValueError, match="Duplicate"):
        load_review(path, domain, "a" * 64, ontology)


def test_casefold_not_just_exact_collision(reviewed):
    path, payload, domain, ontology = reviewed
    payload["entity_types"]["type:0"]["native_name"] = "BusinessTYPE1"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="colliding"):
        load_review(path, domain, "a" * 64, ontology)


def test_crosswalk_endpoints_must_also_match(reviewed):
    path, _, domain, ontology = reviewed
    changed = copy.deepcopy(ontology)
    changed["relationship_types"][0]["allowed_target_semantic_type_ids"] = ["type:11"]
    with pytest.raises(ValueError, match="crosswalk"):
        load_review(path, domain, "a" * 64, changed)


@pytest.mark.parametrize("command", ["repair-ontology-names", "repair-graph-labels"])
def test_public_cli_exposes_review_input(command):
    from click.testing import CliRunner
    from fabric_kg_builder.cli import cli

    result = CliRunner().invoke(cli, ["app", command, "--help"])
    assert result.exit_code == 0
    assert "--naming-review" in result.output
