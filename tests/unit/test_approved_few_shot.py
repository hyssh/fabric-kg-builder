"""Offline formatting fixtures never become actual source evidence."""

import copy
import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain.hierarchy import build_type_hierarchy_closure
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.enrichment import approved_few_shot as shots
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.schema2_extraction import RawCandidateResponse
from tests.unit.test_approved_reextraction import _config, _options, _sdk
from tests.unit.test_schema2_extraction import _domain as _base_domain
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401


def _domain():
    from fabric_kg_builder.domain.models import DomainPropertyV2

    contract = _base_domain()
    entities = [
        item.model_copy(update={"declared_properties": [
            DomainPropertyV2(property_id=f"property:example-{index}", display_name=f"Example {index}", value_type="string"),
        ]}) for index, item in enumerate(contract.candidate_model.entity_types)
    ]
    model = contract.candidate_model.model_copy(update={"entity_types": entities})
    return contract.model_copy(update={
        "candidate_model": model,
        "hierarchy_closure": build_type_hierarchy_closure(entities, model.relationship_types),
    })


def _replacement(contract):
    examples = shots.generate_examples(contract)
    # A semantically equivalent fixture with different absolute source offsets.
    examples[0]["slice_start"] += 13
    examples[0]["slice_end"] += 13
    for record in examples[0]["response"]["candidates"]:
        for anchor in record.get("anchors", [record.get("anchor")]):
            if anchor is not None:
                anchor["span_start"] += 13
                anchor["span_end"] += 13
    return examples


def test_examples_are_complete_schema_valid_deterministic_and_grounded():
    contract = _domain()
    first = shots.generate_examples(contract)
    assert first == shots.generate_examples(contract)
    examples = shots.validate_examples(contract, first)
    for example in examples:
        RawCandidateResponse.model_validate(example["response"])
    records = examples[0]["response"]["candidates"]
    assert {item["candidate_kind"] for item in records} == {"entity", "property", "relationship"}
    assert examples[1]["response"] == {"candidates": []}
    assert all(not item["stable_source_identity"] for item in records if item["candidate_kind"] == "entity")
    assert any(item["identity_key"] for item in records if item["candidate_kind"] == "entity")
    rendered, validated = shots.render_system_prompt(core.SYSTEM_PROMPT, contract)
    assert shots.PLACEHOLDER not in rendered
    assert json.dumps(validated, separators=(",", ":"), ensure_ascii=False, sort_keys=True) in rendered
    assert "..." not in rendered


@pytest.mark.parametrize("change", [
    "empty", "object", "null", "not-synthetic", "numeric-synthetic", "schema", "bounds", "quote", "evidence",
    "identity", "local-id", "type", "owner", "property", "scalar", "predicate",
    "endpoint", "direction", "no-abstention", "placeholder",
])
def test_invalid_replacements_fail_closed(change):
    contract = _domain()
    examples = shots.generate_examples(contract)
    records = examples[0]["response"]["candidates"]
    entity = next(item for item in records if item["candidate_kind"] == "entity")
    prop = next(item for item in records if item["candidate_kind"] == "property")
    rel = next(item for item in records if item["candidate_kind"] == "relationship")
    if change == "empty":
        examples = []
    elif change == "object":
        examples = {}
    elif change == "null":
        examples = None
    elif change == "not-synthetic":
        examples[0]["synthetic"] = False
    elif change == "numeric-synthetic":
        examples[0]["synthetic"] = 1
    elif change == "schema":
        entity["candidate_kind"] = entity["observed_type"]
    elif change == "bounds":
        examples[0]["slice_end"] += 1
    elif change == "quote":
        entity["anchors"][0]["quote"] = "Not the source"
    elif change == "evidence":
        entity["anchors"][0]["model_authored_evidence_id"] = "invented"
    elif change == "identity":
        entity["identity_key"] = {"unknown": "unsupported"}
    elif change == "local-id":
        records.append(copy.deepcopy(entity))
    elif change == "type":
        entity["observed_type"] = "semantic-type:not-approved"
    elif change == "owner":
        prop["owner_local_id"] = "missing"
    elif change == "property":
        prop["observed_property"] = "property:missing"
    elif change == "scalar":
        prop["normalized_value"] = {}
    elif change == "predicate":
        rel["observed_predicate"] = "relationship:missing"
    elif change == "endpoint":
        rel["target_local_id"] = "missing"
    elif change == "direction":
        rel["direction"] = "reverse"
    elif change == "no-abstention":
        examples[1] = copy.deepcopy(examples[0])
    else:
        examples[0]["source_text"] = shots.PLACEHOLDER
    with pytest.raises(ValueError, match="APPROVED_FEW_SHOT_INVALID"):
        shots.validate_examples(contract, examples)


@pytest.mark.parametrize("template", ["no slot", "{{a few shot}} {{a few shot}}"])
def test_invalid_template_fails_closed(template):
    with pytest.raises(ValueError, match="exactly one"):
        shots.render_system_prompt(template, _domain())


def test_all_scalar_types_and_abstract_endpoints_use_active_contract():
    from fabric_kg_builder.domain.models import DomainPropertyV2

    contract = _domain()
    types = contract.candidate_model.entity_types
    leaf = next(item for item in types if not item.abstract)
    properties = [
        DomainPropertyV2(property_id=f"property:example-{kind}", display_name=f"Example {kind}", value_type=kind)
        for kind in ("string", "integer", "number", "boolean", "date", "datetime")
    ]
    entities = [
        item.model_copy(update={"declared_properties": [*item.declared_properties, *properties]})
        if item.type_id == leaf.type_id else item for item in types
    ]
    model = contract.candidate_model.model_copy(update={"entity_types": entities})
    contract = contract.model_copy(update={
        "candidate_model": model,
        "hierarchy_closure": build_type_hierarchy_closure(entities, model.relationship_types),
    })
    validated = shots.validate_examples(contract, shots.generate_examples(contract))
    records = validated[0]["response"]["candidates"]
    vocabulary = shots.compile_closed_vocabulary(contract)
    owners = {item["local_id"]: item["observed_type"] for item in records if item["candidate_kind"] == "entity"}
    assert {prop.value_type for prop in properties} == {
        vocabulary.properties_by_type_and_alias[owners[item["owner_local_id"]]][item["observed_property"].casefold()].value_type
        for item in records if item["candidate_kind"] == "property"
    }
    assert all(not next(item for item in entities if item.type_id == record["observed_type"]).abstract
               for record in records if record["candidate_kind"] == "entity")


def test_contract_without_compatible_relationship_does_not_invent_one():
    contract = _domain()
    model = contract.candidate_model.model_copy(update={"relationship_types": []})
    contract = contract.model_copy(update={
        "candidate_model": model,
        "hierarchy_closure": build_type_hierarchy_closure(model.entity_types, []),
    })
    records = shots.validate_examples(contract, shots.generate_examples(contract))[0]["response"]["candidates"]
    assert not any(item["candidate_kind"] == "relationship" for item in records)


def test_example_key_order_and_vocabulary_order_do_not_change_rendering():
    contract = _domain()
    replacement = shots.generate_examples(contract)
    reordered = json.loads(json.dumps(replacement, sort_keys=True))
    assert shots.render_system_prompt(core.SYSTEM_PROMPT, contract, few_shot=replacement)[0] == (
        shots.render_system_prompt(core.SYSTEM_PROMPT, contract, few_shot=reordered)[0]
    )
    model = contract.candidate_model.model_copy(update={
        "entity_types": list(reversed(contract.candidate_model.entity_types)),
        "relationship_types": list(reversed(contract.candidate_model.relationship_types)),
    })
    assert shots.generate_examples(contract.model_copy(update={"candidate_model": model})) == replacement


def test_synthetic_quotes_cannot_mint_evidence_for_real_source():
    from fabric_kg_builder.enrichment.schema2_evidence import (
        ProposedOccurrenceAnchor, verify_and_mint_extraction_span,
    )
    from tests.unit.test_schema2_evidence import _NOW, _unit

    real_source = _unit("A real source contains no synthetic fixture observations.")
    for example in shots.generate_examples(_domain()):
        for record in example["response"]["candidates"]:
            for anchor in record.get("anchors", [record.get("anchor")]):
                if anchor is not None:
                    result = verify_and_mint_extraction_span(
                        source_unit=real_source, anchor=ProposedOccurrenceAnchor(**anchor),
                        verified_at_utc=_NOW,
                    )
                    assert result.span is None
                    assert result.reason_codes


def test_documented_json_has_valid_envelopes_references_and_offsets():
    from pathlib import Path

    document = (Path(__file__).resolve().parents[2] / "docs/specs/SPEC-APPROVED-EXTRACTION-PROMPT.md").read_text()
    section = document.split("### Complete JSON example (synthetic fixture, not production facts)", 1)[1]
    examples = json.loads(section.split("```json\n", 1)[1].split("\n```", 1)[0])
    for example in examples:
        assert len(example["source_text"]) == example["slice_end"] - example["slice_start"]
        RawCandidateResponse.model_validate(example["response"])
        entities = {record["local_id"] for record in example["response"]["candidates"]
                    if record["candidate_kind"] == "entity"}
        for record in example["response"]["candidates"]:
            for key in ("owner_local_id", "source_local_id", "target_local_id"):
                if key in record:
                    assert record[key] in entities
            for anchor in record.get("anchors", [record.get("anchor")]):
                assert example["source_text"][anchor["span_start"]:anchor["span_end"]] == anchor["quote"]


def test_fresh_replacement_is_sent_sealed_and_resume_detects_drift(integrated_case):
    options = _options(integrated_case)
    contract = load_domain_contract(options["domain_path"])
    replacement = _replacement(contract)
    sdk, requests = _sdk()
    result = core.run_approved_reextraction(
        **options, few_shot=replacement, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    rendered, _ = shots.render_system_prompt(core.SYSTEM_PROMPT, contract, few_shot=replacement)
    assert requests[0]["messages"][0]["content"].startswith(rendered)
    assert "Synthetic example" not in requests[0]["messages"][1]["content"]
    assert core.run_approved_reextraction(
        **options, few_shot=replacement, resume=True, client_factory=lambda: pytest.fail("resume called SDK"),
    ) == result
    with pytest.raises(ValueError, match="FINGERPRINT_DRIFT"):
        core.run_approved_reextraction(**options, resume=True, dry_run=True)
    for leaf in (options["state_root"] / "checkpoint-leaves").glob("*.json"):
        assert json.loads(leaf.read_text())["raw_candidate_count"] == 0


def test_cli_replacement_is_approved_only_and_validated(integrated_case, monkeypatch):
    from types import SimpleNamespace
    from fabric_kg_builder.config import loader

    options = _options(integrated_case)
    file = options["state_root"].parent / "examples.json"
    monkeypatch.setattr(loader, "load_config", lambda **_: SimpleNamespace(foundry=_config()))
    args = [
        "enrich", "--input", str(options["source_path"]), "--domain-file", str(options["domain_path"]),
        "--l1-state", str(options["l1_state_root"]), "--l2-state", str(options["state_root"]),
        "--window-run", str(options["window_run_path"]), "--reextract-approved", "--max-calls", "1", "--dry-run",
    ]
    default = CliRunner().invoke(cli, args)
    assert default.exit_code == 0, default.output
    replacement = _replacement(load_domain_contract(options["domain_path"]))
    file.write_text(json.dumps(replacement))
    result = CliRunner().invoke(cli, [*args, "--approved-few-shot", str(file)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["fingerprint"] != json.loads(default.output)["fingerprint"]
    for invalid in ("null", "[]", "{}", "not json"):
        file.write_text(invalid)
        result = CliRunner().invoke(cli, [*args, "--approved-few-shot", str(file)])
        assert result.exit_code != 0
        assert not options["state_root"].exists()
    result = CliRunner().invoke(cli, [
        "enrich", "--input", str(options["source_path"]), "--approved-few-shot", str(file),
    ])
    assert result.exit_code != 0 and "require --reextract-approved" in result.output


def test_optional_property_growth_does_not_expand_examples_or_lose_identity():
    from fabric_kg_builder.domain.models import DomainPropertyV2

    contract = _domain()
    baseline, examples = shots.render_system_prompt(core.SYSTEM_PROMPT, contract)
    entities = [
        item.model_copy(update={"declared_properties": [
            *item.declared_properties,
            *[DomainPropertyV2(property_id=f"property:zz-optional-{i}", display_name=f"Optional {i}", value_type="string")
              for i in range(30)],
        ]}) for item in contract.candidate_model.entity_types
    ]
    model = contract.candidate_model.model_copy(update={"entity_types": entities})
    expanded = contract.model_copy(update={
        "candidate_model": model,
        "hierarchy_closure": build_type_hierarchy_closure(entities, model.relationship_types),
    })
    rendered, compact = shots.render_system_prompt(core.SYSTEM_PROMPT, expanded)
    assert compact == examples
    assert rendered == baseline
    pretty = json.dumps(examples, indent=2, ensure_ascii=False, sort_keys=True)
    serialized = json.dumps(compact, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    assert len(serialized) < len(pretty) * 0.75
    assert shots.validate_examples(expanded, compact)
