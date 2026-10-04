"""Audited working mutations, replay and public inspection; no cloud calls."""

import copy
import hashlib
import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain import document_schema
from fabric_kg_builder.domain.document_schema_evolution import transitions
from fabric_kg_builder.domain.window_design_context import window_design_context
from fabric_kg_builder.domain.window_run import load_windowed_run, run_windowed, windowed_history, windowed_status
from tests.unit.test_document_schema import DocumentModel, config
from tests.unit.test_window_run import budget, inputs


def entity(identifier, name, **extra):
    return {
        "concept_id": identifier, "kind": "entity", "name": name,
        "definition": f"A reusable {name.lower()} type.", "identity_policy": {"mode": "unresolved"},
        **extra,
    }


def mutation(payload, action, concept=None, concept_id=None, **extra):
    section = payload["document"]["sections"][0]
    return {
        "action": action, "concept": concept, "concept_id": concept_id,
        "reason": "Reconcile definitions across the document set.",
        "witnesses": [{"ref": section["ref"], "quote": section["text"][:80]}],
        "layer": "domain", "scope_change": "additive" if action == "add_concept" else "broadening",
        "generalization_reason": "Retain reusable roles instead of document-specific classes.",
        **extra,
    }


CORRECTION = {
    "change_basis": "instance_as_type",
    "prior_schema_impact": "Retain the earlier model as a Product instance and explicitly retarget its dependent definitions.",
}


def initial_concepts():
    return [
        entity("specific", "Specific Model"), entity("part", "Part"),
        entity("child", "Reusable Child", parent_type_id="specific"),
        {"concept_id": "contains", "kind": "relationship", "name": "Contains",
         "definition": "A product contains a part.", "source_type_ids": ["specific"],
         "target_type_ids": ["part"], "identity_policy": {"mode": "unresolved", "context_policy": "Assembly"}},
        {"concept_id": "name", "kind": "property", "name": "Name",
         "definition": "The product designation.", "owner_type_ids": ["specific"], "value_type": "string"},
    ]


def correction_batch(payload):
    known = {row["concept_id"]: row for row in payload["schema"]["concepts"]}
    return [
        mutation(payload, "add_concept", entity("product", "Product")),
        mutation(payload, "update_concept", {**known["child"], "parent_type_id": "product"}, **CORRECTION),
        mutation(payload, "update_concept", {**known["contains"], "source_type_ids": ["product"]}, **CORRECTION),
        mutation(payload, "update_concept", {**known["name"], "owner_type_ids": ["product"]}, **CORRECTION),
        mutation(payload, "delete_concept", concept_id="specific", **CORRECTION),
    ]


def test_normalization_policy_is_versioned_without_changing_historical_prompts():
    from fabric_kg_builder.domain.document_schema_evolution import NORMALIZATION_POLICY

    historical_hashes = {
        "whole-document-schema/1.0.0": "fc8417c9985cb212b07b3d5adb65514a46708e1d64b8bafe0f6464aca60d4772",
        "whole-document-schema/1.1.0": "f6d1a54a590a52445e897e0ea2ce540f2f59be402da0c32f428eb66dd6385292",
        "whole-document-schema/1.2.0": "4ef098c1038bdb1654b9fc3f2dd305e676cb69ed5a966c1c539d089b1769a50c",
    }
    for version, expected in historical_hashes.items():
        assert hashlib.sha256(document_schema.system_prompt(version).encode()).hexdigest() == expected
    current = document_schema.system_prompt(document_schema.PROMPT_VERSION)
    previous = document_schema.system_prompt(document_schema.EVOLUTION_PROMPT_VERSION)
    assert current.count(NORMALIZATION_POLICY) == 1
    assert current.replace(NORMALIZATION_POLICY, "") == previous
    assert document_schema.response_model(document_schema.PROMPT_VERSION) is document_schema.response_model(
        document_schema.EVOLUTION_PROMPT_VERSION)


@pytest.mark.parametrize("version", document_schema.EVOLUTION_PROMPT_VERSIONS)
def test_serial_mutations_keep_source_before_after_and_unchanged_revision(tmp_path, version):
    data = inputs(tmp_path, files=4, paragraphs=1)

    def respond(payload):
        checkpoint = payload["schema"]["version"]
        if checkpoint == 0:
            proposals = [mutation(payload, "add_concept", concept) for concept in initial_concepts()]
        elif checkpoint == 1:
            assert payload["schema_revision"] == 1
            assert payload["schema_change_history"][0]["changes"][0]["evidence"]
            proposals = correction_batch(payload)
        elif checkpoint == 2:
            assert payload["retired_concept_ids"] == ["specific"]
            assert "specific" not in payload["schema_layers"]
            proposals = [
                mutation(payload, "delete_concept", concept_id=key,
                         change_basis="correction", prior_schema_impact="Remove an unsupported optional definition.")
                for key in ("contains", "name")
            ]
        else:
            assert payload["schema_revision"] == 3
            proposals = []
        return {"schema_proposals": proposals, "pending": []}

    root = tmp_path / "windows"
    model = DocumentModel(respond)
    result = run_windowed(inputs=data, output_dir=root, config=config(prompt_version=version), budget=budget(), client=model)
    assert result.state == "complete"
    assert all(request["system"].startswith(document_schema.system_prompt(version)) for request in model.requests)
    history = transitions(result.logs)
    assert [row["schema_revision_after"] for row in history] == [1, 2, 3, 3]
    assert [row["checkpoint_after"] for row in history] == [1, 2, 3, 4]
    assert [c.concept_id for c in result.final_snapshot.concepts] == ["part", "child", "product"]
    assert [d.change.action for d in result.logs[1].decisions] == [
        "add_concept", "update_concept", "update_concept", "update_concept", "delete_concept"]
    assert {row["kind"] for row in history[2]["changes"]} == {"property", "relationship"}
    units = {unit.source_unit_id: unit for unit in data.prepared.source_units}
    for row in history:
        assert row["document"] and row["source_file_id"]
        for change in row["changes"]:
            assert change["status"] == "accepted_working" and change["reason"]
            if change["action"] == "add_concept":
                assert change["before"] is None and change["after"] is not None
            elif change["action"] == "delete_concept":
                assert change["before"] is not None and change["after"] is None
            for witness in change["evidence"]:
                unit = units[witness["source_unit_id"]]
                assert witness["source_file_id"] == row["source_file_id"] == unit.source_file_id
                assert unit.text[witness["span_start"]:witness["span_end"]] == witness["quote"]
                assert witness["source_text_hash"] == unit.text_content_hash
    assert history[1]["changes"][2]["before"]["source_type_ids"] == ["specific"]
    assert history[1]["changes"][2]["after"]["source_type_ids"] == ["product"]
    assert windowed_status(root)["schema_revision"] == 3
    assert windowed_status(root)["schema_version"] == 4
    context = window_design_context(result.run)
    assert set(context["schema_layers"]) == {"part", "child", "product"}
    assert context["schema_change_history"] == history
    output = CliRunner().invoke(cli, ["domain", "window-run-history", "--state", str(root), "--changes-only"])
    assert output.exit_code == 0, output.output
    assert json.loads(output.output) == history
    assert windowed_history(root, changes_only=True) == history
    assert load_windowed_run(root) == result.run
    replay = run_windowed(inputs=data, output_dir=root, config=config(prompt_version=version), budget=budget(max_calls=0))
    assert replay.run == result.run and replay.model_call_count == 0


@pytest.mark.parametrize("invalid", ["dependency", "quote", "impact", "unknown", "duplicate", "absent_basis"])
def test_failed_mutation_rejects_entire_batch_without_changing_actual_schema(tmp_path, invalid):
    data = inputs(tmp_path, files=2, paragraphs=1)

    def respond(payload):
        if payload["schema"]["version"] == 0:
            return {"schema_proposals": [mutation(payload, "add_concept", c) for c in initial_concepts()]}
        batch = correction_batch(payload)
        if invalid == "dependency":
            batch.pop(2)
        elif invalid == "quote":
            batch[-1]["witnesses"][0]["quote"] = "not in the source"
        elif invalid == "impact":
            del batch[-1]["prior_schema_impact"]
        elif invalid == "unknown":
            batch[-1]["concept_id"] = "unknown"
        elif invalid == "duplicate":
            batch.append(copy.deepcopy(batch[-1]))
        else:
            batch[-1]["change_basis"] = None
        return {"schema_proposals": batch}

    root = tmp_path / "windows"
    result = run_windowed(inputs=data, output_dir=root, config=config(), budget=budget(), client=DocumentModel(respond))
    history = transitions(result.logs)
    assert history[-1]["status"] == "rejected"
    assert history[-1]["schema_revision_after"] == 1
    assert all(row["before"] == row["after"] and row["rejection_reason"] for row in history[-1]["changes"])
    assert not result.logs[-1].decisions
    assert {c.concept_id for c in result.final_snapshot.concepts} == {c["concept_id"] for c in initial_concepts()}
    assert load_windowed_run(root) == result.run


def test_retired_id_cannot_reappear_and_noop_alias_does_not_advance_revision(tmp_path):
    data = inputs(tmp_path, files=4, paragraphs=1)

    def respond(payload):
        step = payload["schema"]["version"]
        if step == 0:
            batch = [mutation(payload, "add_concept", entity("retire", "Retired")),
                     mutation(payload, "add_concept", entity("keep", "Retained", aliases=["Keep"]))]
        elif step == 1:
            batch = [mutation(payload, "delete_concept", concept_id="retire", **CORRECTION)]
        elif step == 2:
            batch = [mutation(payload, "add_alias", concept_id="keep", alias="Keep")]
        else:
            batch = [mutation(payload, "add_concept", entity("retire", "Different Meaning"))]
        return {"schema_proposals": batch}

    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config(),
                          budget=budget(), client=DocumentModel(respond))
    assert [row["schema_revision_after"] for row in transitions(result.logs)] == [1, 2, 2, 2]
    assert "retired" in result.logs[-1].diagnostics[0].reason


def test_v11_preserves_legacy_contract_and_has_no_synthetic_audit(tmp_path):
    data = inputs(tmp_path, files=2, paragraphs=1)
    cfg = config(prompt_version=document_schema.GENERALIZED_PROMPT_VERSION)
    root = tmp_path / "windows"
    result = run_windowed(inputs=data, output_dir=root, config=cfg, budget=budget(), client=DocumentModel())
    assert "Do not delete old concepts or aliases." in document_schema.system_prompt(cfg.prompt_version)
    assert document_schema.response_model(cfg.prompt_version) is document_schema.GeneralizedDocumentSchemaResponse
    assert result.logs[1].decisions[0].change.action == "add_concept"
    assert not transitions(result.logs)
    assert "schema_revision" not in windowed_status(root)
    assert "schema_layers" in window_design_context(result.run)
    assert load_windowed_run(root) == result.run
    assert run_windowed(inputs=data, output_dir=root, config=cfg, budget=budget(max_calls=0)).run == result.run
    with pytest.raises(ValueError, match="full history"):
        windowed_history(root, changes_only=True)
