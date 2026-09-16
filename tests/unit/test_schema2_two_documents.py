"""LOCAL OFFLINE two-document regression; deterministic stubs do not assess GPT quality.

Set FKG_OFFLINE_REGRESSION_ROOT to retain a run in a specific artifact directory.
Existing artifacts are never overwritten. No OCR, deployment, or network is used.
Whole-document mode requires a populated TIKTOKEN_CACHE_DIR for offline accounting.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import socket
from types import SimpleNamespace
from uuid import uuid4

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.domain import document_schema
from fabric_kg_builder.domain.discovery import prepare_discovery_corpus
from fabric_kg_builder.domain.window_run import load_windowed_run
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.domain.stage import preflight_l1_inputs
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.schema2_validation_stage import load_l3_inputs, run_l3
from fabric_kg_builder.enrichment.window_run_reuse import prepare_window_run_reuse
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from fabric_kg_builder.sources.preparation import indexed_corpus_reader
from tests.unit.test_approved_reextraction import _config, _no_source_reads, _sdk
from tests.unit.test_domain_discovery_cli import _invoke, _paths
from tests.unit.test_window_run_approved_cli import IntegratedModel, _approve_integrated


def _anchor(text, quote, offset):
    start = text.index(quote) + offset
    return {
        "span_start": start, "span_end": start + len(quote), "quote": quote,
        "model_authored_evidence_id": None,
    }


def _observations(text, offset, record_type, subject_type, predicate, voltage):
    suffix = "Alpha" if "Alpha" in text else "Beta"
    labels = {"record-1": f"governed record {suffix}", "subject-1": f"governed subject {suffix}"}
    numeric = re.search(r"\d+\.\d+", text).group()
    candidates = [
        {
            "candidate_kind": "entity", "local_id": key, "observed_type": type_id,
            "label": label, "anchors": [_anchor(text, label, offset)],
            "stable_source_identity": None, "identity_key": {},
        }
        for (key, label), type_id in zip(labels.items(), (record_type, subject_type))
    ]
    candidates.extend([
        {
            "candidate_kind": "relationship", "source_local_id": "record-1",
            "target_local_id": "subject-1", "observed_predicate": predicate,
            "direction": "source_to_target", "governed_context": labels["record-1"],
            "member_role_id": None, "member_order": None,
            "anchor": _anchor(text, text.strip(), offset),
        },
        {
            "candidate_kind": "property", "owner_local_id": "subject-1",
            "observed_property": voltage, "value": float(numeric), "normalized_value": float(numeric),
            "temporal_key": None,
            "anchor": _anchor(text, f"{labels['subject-1']} at {numeric} volts", offset),
        },
    ])
    return candidates


class TwoDocumentDesignModel(IntegratedModel):
    """Discovery/design logical calls only, injected via the existing CLI seam."""

    def __init__(self):
        super().__init__()
        self.offline_requests = []

    def complete_json(self, **request):
        self.offline_requests.append(request)
        result = super().complete_json(**request)
        if "candidates" not in result:
            return result
        payload = json.loads(request["user"])["input"]
        if "repair" not in payload:
            result["candidates"] = _observations(
                payload["text"], payload["offset_base"],
                "Service Record", "Service Subject", "Describes", "Observed Voltage",
            )
        return result


class GeneralizedDocumentDesignModel(TwoDocumentDesignModel, FoundryClient):
    """OFFLINE provider seam; production SDK envelope, guards and admission run."""

    capability_profile = {
        "deployment": "offline-generalized-gpt54", "model_name": "gpt-5.4",
        "model_version": "2026-03-05", "deployment_sku": "GlobalStandard",
        "endpoint": "https://offline.invalid", "context_tokens": 1_050_000,
        "max_input_tokens": 922_000, "max_output_tokens": 128_000,
        "source": "offline-explicit-profile-not-a-live-deployment",
    }

    def __init__(self):
        super().__init__()
        self.document_requests = []
        self.document_responses = []
        self._config = _config().model_copy(update={
            "chat_model": "gpt-5.4", "chat_deployment": self.capability_profile["deployment"],
        })
        sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self._create)))

        def with_options(**kwargs):
            assert kwargs == {"max_retries": 0}
            return sdk

        sdk.with_options = with_options
        self._client = sdk

    def _create(self, **request):
        self.document_requests.append(deepcopy(request))
        payload = json.loads(request["messages"][1]["content"])["input"]
        section = payload["document"]["sections"][0]
        known = {row["concept_id"]: row for row in payload["schema"]["concepts"]}
        concepts = [
            {"concept_id": "working:record", "kind": "entity", "name": "Service Record",
             "definition": "A source service record.", "identity_policy": {"mode": "unresolved"}},
            {"concept_id": "working:subject", "kind": "entity", "name": "Service Subject",
             "definition": "A common service subject.", "identity_policy": {"mode": "unresolved"}},
            {"concept_id": "working:describes", "kind": "relationship", "name": "Describes",
             "definition": "Record describes a subject.", "source_type_ids": ["working:record"],
             "target_type_ids": ["working:subject"],
             "identity_policy": {"mode": "unresolved", "context_policy": "source record"}},
            {"concept_id": "working:voltage", "kind": "property", "name": "Observed Voltage",
             "definition": "Subject voltage.", "owner_type_ids": ["working:subject"], "value_type": "number"},
        ]
        if known:
            concepts = [{
                **known["working:subject"],
                "definition": "A common service subject described by records across independently named service cases.",
            }]
        response = {"schema_proposals": [
            {
                "action": "update_concept" if known else "add_concept", "concept": concept,
                "reason": "Reconcile a reusable service role with this complete document.",
                "witnesses": [{"ref": section["ref"], "quote": section["text"]}],
                "layer": "common" if concept["concept_id"] == "working:subject" else "domain",
                "scope_change": "broadening" if known else "additive",
                "generalization_reason": "Document labels are instance values; this definition covers other service cases.",
            }
            for concept in concepts
        ], "pending": []}
        self.document_responses.append(deepcopy(response))
        return SimpleNamespace(
            model="gpt-5.4-2026-03-05",
            choices=[SimpleNamespace(message=SimpleNamespace(content=canonical_json(response)), finish_reason="stop")],
        )


def _approved_response(request):
    payload = json.loads(request["messages"][1]["content"])
    relationship = next(row for row in payload["relationship_types"] if row["display_name"] == "Describes")
    types = {row["type_id"]: row for row in payload["entity_types"]}
    record = types[relationship["source_type_ids"][0]]
    subject = types[relationship["target_type_ids"][0]]
    numeric = next(row for row in subject["effective_properties"] if row["value_type"] == "number")
    text, offset = payload["source_text"], payload["source_identity"]["slice_start"]
    candidates = _observations(
        text, offset, record["type_id"], subject["type_id"],
        relationship["relationship_type_id"], numeric["property_id"],
    )
    for candidate, definition in zip(candidates[:2], (record, subject)):
        label = candidate["label"]
        candidate["identity_key"] = {
            key: label for key in definition["identity_key_policy"]["business_key_fields"]
        }
        for prop in definition["effective_properties"]:
            if prop["value_type"] == "string":
                candidates.append({
                    "candidate_kind": "property", "owner_local_id": candidate["local_id"],
                    "observed_property": prop["property_id"], "value": label, "normalized_value": label,
                    "temporal_key": None, "anchor": _anchor(text, label, offset),
                })
    # This is the model response envelope, not a source-text echo or replay.
    return {"candidates": candidates}


def _snapshot(*roots):
    return {
        str(path): canonical_sha256({"bytes": path.read_bytes().hex()})
        for root in roots for path in (root.rglob("*") if root.is_dir() else [root])
        if path.is_file()
    }


def _trace(record_id, l1, domain, l2, *, l4=None, l3=None):
    return _invoke([
        "lineage", "trace", record_id, "--l2-state", str(l2),
        "--l1-state", str(l1), "--domain", str(domain), "--format", "json",
        *(["--l4-run", str(l4), "--l3-root", str(l3)] if l4 is not None else []),
    ])


def run_two_document_regression(root: Path, monkeypatch, *, discovery_mode="chunked"):
    """Run real local stages with two independent deterministic model seams."""
    root.mkdir(parents=True, exist_ok=False)
    network_attempts = []

    def deny_network(*args, **kwargs):
        network_attempts.append("attempted")
        pytest.fail("OFFLINE regression attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    source, intake, _, _, _ = _paths(root, count=2)
    for index, suffix in enumerate(("Alpha", "Beta")):
        old = source / f"records-{index}.html"
        old.rename(source / f"service-record-{suffix.lower()}.html")
        (source / f"service-record-{suffix.lower()}.html").write_text(
            f"<p>A governed record {suffix} describes a governed subject {suffix} at {80.5 + index} volts.</p>",
            encoding="utf-8",
        )
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=json.loads(intake.read_text()),
        project_id="project:records", run_id="run:offline-two-document-prepared",
        model_version="offline-deterministic", model_hash=canonical_sha256({"offline": True}),
    )
    prepared = prepare_discovery_corpus(
        preflight, reader=indexed_corpus_reader(preflight.corpus, source, project_id="project:records"),
    )
    prepared_path = root / "prepared.json"
    prepared_path.write_text(canonical_json(prepared), encoding="utf-8")
    source_before = _snapshot(source, prepared_path, intake)
    windows = root / "windows"
    whole_document = discovery_mode == "whole-document"
    assert discovery_mode in {"chunked", "whole-document"}
    model = GeneralizedDocumentDesignModel() if whole_document else TwoDocumentDesignModel()
    if whole_document:
        profile_path = root / "capability-profile.json"
        profile_path.write_text(canonical_json(model.capability_profile), encoding="utf-8")
        discovery_options = ["--discovery-mode", "whole-document", "--capability-profile", str(profile_path)]
    else:
        discovery_options = ["--discovery-mode", "chunked", "--schema-policy", "concepts"]
    window_result = _invoke([
        "domain", "window-run", "--prepared", str(prepared_path), "--intake", str(intake),
        *discovery_options, "--out-state", str(windows), "--window-size", "1",
        "--concurrency", "1", "--max-tokens", "1000000", "--max-calls", "2" if whole_document else "8",
        "--max-repair-calls", "8", "--live",
    ], model=model)
    assert window_result["status"] == "complete"
    assert source_before == _snapshot(source, prepared_path, intake)
    if whole_document:
        working = load_windowed_run(windows)
        assert working.config.prompt_version == document_schema.PROMPT_VERSION == "whole-document-schema/1.3.0"
        assert len(working.logs) == len(model.document_requests) == 2
        assert len(list((windows / "physical-requests").glob("*.json"))) == 2
        assert canonical_sha256(working.prepared) == canonical_sha256(prepared)
        assert {
            json.loads(request["messages"][1]["content"])["input"]["document"]["name"]
            for request in model.document_requests
        } == {entry.relative_source_ref for entry in prepared.corpus.entries}
        assert working.final_snapshot.version == 2
        assert not working.final_mapping.records
        assert all(not chunk.raw_response["candidates"] for chunk in working.chunks)
        concepts = {row.concept_id: row.model_dump(mode="json") for row in working.final_snapshot.concepts}
        expected_layers = {
            "working:record": "domain", "working:subject": "common",
            "working:describes": "domain", "working:voltage": "domain",
        }
        assert set(concepts) == {"working:record", "working:subject", "working:describes", "working:voltage"}
        assert "across independently named service cases" in concepts["working:subject"]["definition"]
        assert not re.search(r"Alpha|Beta", canonical_json(concepts))
        for index, (request, log) in enumerate(zip(model.document_requests, working.logs)):
            payload = json.loads(request["messages"][1]["content"])["input"]
            assert request["model"] == model.capability_profile["deployment"]
            assert log.exchanges[0].response.payload["provider_model"] == "gpt-5.4-2026-03-05"
            assert payload["authority"] == "schema_discovery_only_not_instance_extraction"
            assert payload["context"]["intake"] == json.loads(intake.read_text())
            assert payload["schema"]["version"] == index
            expected_file = next(entry.source_file_id for entry in prepared.corpus.entries
                                 if entry.relative_source_ref == payload["document"]["name"])
            expected_units = sorted(
                (unit for unit in prepared.source_units if unit.source_file_id == expected_file),
                key=lambda unit: (unit.ordinal, unit.source_unit_id),
            )
            assert [row["text"] for row in payload["document"]["sections"]] == [unit.text for unit in expected_units]
            response_schema = log.exchanges[0].request.payload["request"]["json_schema"]
            assert "candidates" not in response_schema["properties"]
            proposal_schema = response_schema["$defs"]["EvolutionProposal"]
            assert {"layer", "generalization_reason", "scope_change"} <= set(proposal_schema["required"])
            assert "CROSS-DOCUMENT GENERALIZATION" in request["messages"][0]["content"]
            assert set(model.document_responses[index]) == {"schema_proposals", "pending"}
            if index == 0:
                assert not payload["schema"]["concepts"]
                assert not payload["schema_layers"]
            else:
                assert payload["schema_layers"] == expected_layers
                prior = {row["concept_id"]: row for row in payload["schema"]["concepts"]}
                assert set(prior) == set(concepts)
                assert prior["working:subject"]["definition"] == "A common service subject."
                assert all(prior[key] == value for key, value in concepts.items() if key != "working:subject")
                assert model.document_responses[index]["schema_proposals"][0]["action"] == "update_concept"
    else:
        observations = [
            json.loads(request["user"])["input"] for request in model.offline_requests
            if "candidates" in request["json_schema"]["properties"]
            and "repair" not in json.loads(request["user"])["input"]
        ]
        assert len(observations) == 2
        assert not observations[0]["schema"]["concepts"]
        assert observations[1]["schema"]["concepts"]
        assert {"80.5", "81.5"} == {re.search(r"\d+\.\d+", row["text"]).group() for row in observations}

    # Working-schema discoveries are not an authority for instance data lineage.
    blocked = CliRunner().invoke(cli, [
        "lineage", "trace", "record-1", "--l2-state", str(windows),
        "--l1-state", str(root / "l1"), "--domain", str(root / "domain.yaml"), "--format", "json",
    ])
    assert blocked.exit_code != 0 and "LINEAGE_SCHEMA_NOT_FROZEN" in blocked.output
    l1, domain, _ = _approve_integrated(root, source, intake, windows, model)
    if whole_document:
        design_marker = "Representative integrated-window design context (full local authority retained):\n"
        design_contexts = [
            json.JSONDecoder().raw_decode(request["user"].split(design_marker, 1)[1])[0]
            for request in model.offline_requests if design_marker in request["user"]
        ]
        assert design_contexts
        assert all(context["schema_layers"] == expected_layers for context in design_contexts)
        with pytest.raises(ValueError, match="WHOLE_DOCUMENT_SCHEMA_REQUIRES_REEXTRACTION"):
            prepare_window_run_reuse(windows, source, l1, domain)
    contract = load_domain_contract(domain)
    frozen_hash = contract.approval.contract_hash
    frozen_before = _snapshot(windows, l1, domain)

    # These guards cover reextraction and every subsequent stage; source parsing
    # was permitted only during corpus preparation and the final L1 compilation.
    _no_source_reads(monkeypatch)
    sdk, requests = _sdk(_approved_response)
    l2 = root / "fresh-l2"
    options = dict(
        source_path=source, l1_state_root=l1, domain_path=domain, state_root=l2,
        window_run_path=windows, foundry_config=_config(), max_calls=2, max_output_tokens=4096,
    )
    dry = core.run_approved_reextraction(
        **options, dry_run=True, client_factory=lambda: pytest.fail("Dry run constructed client"),
    )
    assert dry["planned_chunks"] == 2 and dry["remote_calls"] == dry["original_source_reads"] == 0
    data_lineage = {
        "enabled_after": "L1_approval",
        "domain_contract_hash": frozen_hash,
        "storage": "L2_candidate_lifecycle_and_source_manifests",
    }
    assert dry["data_lineage"] == data_lineage
    assert not l2.exists()
    extracted = core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    assert extracted["logical_calls"] == extracted["physical_calls"] == len(requests) == 2
    assert extracted["data_lineage"] == data_lineage
    persisted_result = json.loads((l2 / "approved-reextraction-result.json").read_text())
    assert persisted_result == extracted
    assert persisted_result["data_lineage"] == data_lineage
    resumed = core.run_approved_reextraction(
        **options, resume=True, client_factory=lambda: pytest.fail("Exact resume called model"),
    )
    assert resumed == extracted
    assert frozen_before == _snapshot(windows, l1, domain)
    assert load_domain_contract(domain).approval.contract_hash == frozen_hash

    payloads = [json.loads(request["messages"][1]["content"]) for request in requests]
    assert {payload["domain_contract_hash"] for payload in payloads} == {frozen_hash}
    assert len({payload["source_identity"]["source_unit_id"] for payload in payloads}) == 2
    for request, payload in zip(requests, payloads):
        if whole_document:
            source_unit = next(unit for unit in prepared.source_units
                               if unit.source_unit_id == payload["source_identity"]["source_unit_id"])
            assert payload["source_text"] == source_unit.text
        response = _approved_response(request)
        assert set(response) == {"candidates"} and "source_text" not in response
        entities = {row["local_id"]: row for row in response["candidates"] if row["candidate_kind"] == "entity"}
        types = {row["type_id"]: row for row in payload["entity_types"]}
        for row in response["candidates"]:
            anchors = row["anchors"] if row["candidate_kind"] == "entity" else [row["anchor"]]
            for anchor in anchors:
                offset = payload["source_identity"]["slice_start"]
                assert payload["source_text"][anchor["span_start"] - offset:anchor["span_end"] - offset] == anchor["quote"]
            if row["candidate_kind"] == "property":
                owner = types[entities[row["owner_local_id"]]["observed_type"]]
                assert row["observed_property"] in owner["effective_property_ids"]
                assert str(row["value"]) in row["anchor"]["quote"]
                assert entities[row["owner_local_id"]]["label"] in row["anchor"]["quote"]
            elif row["candidate_kind"] == "relationship":
                definition = next(item for item in payload["relationship_types"]
                                  if item["relationship_type_id"] == row["observed_predicate"])
                assert entities[row["source_local_id"]]["observed_type"] in definition["source_type_ids"]
                assert entities[row["target_local_id"]]["observed_type"] in definition["target_type_ids"]

    l3, l4 = root / "l3", root / "l4"
    for args in [
        ["validate-evidence", "--state", str(l3), "--l2-state", str(l2),
         "--l1-state", str(l1), "--domain", str(domain)],
        ["project-serving", "--state", str(l4), "--l3-state", str(l3),
         "--l2-state", str(l2), "--l1-state", str(l1), "--domain", str(domain)],
    ]:
        stage = CliRunner().invoke(cli, args)
        assert stage.exit_code == 0, (stage.output, stage.exception)
    validated = run_l3(state_root=l3, l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    served = run_l4(validated, state_root=l4)
    rows = served.rows
    assert len(rows.semantic_asserted_entities) == 4
    assert len(rows.semantic_asserted_relationships) == 2
    assert len(rows.semantic_asserted_properties) == 6
    assert {json.loads(row["normalized_value_json"]) for row in rows.semantic_asserted_properties
            if row["value_type"] == "number"} == {80.5, 81.5}
    assert all(len(row["label"]) < 40 for row in rows.semantic_asserted_entities)
    assert all(row.evidence_span_ids for row in validated.candidate_results)
    assert all(row.current_state == "asserted" for row in validated.candidate_results)

    inputs = load_l3_inputs(l1_state_root=l1, domain_path=domain, l2_state_root=l2)
    by_document = {}
    for partition in inputs.proposed_partitions.values():
        for candidate in partition:
            unit = inputs.source_units.require(candidate.source_unit_id)
            by_document.setdefault(unit.source_file_id, set()).add(candidate.candidate_id)
    assert len(by_document) == 2
    candidate_sets = list(by_document.values())
    assert all(len(ids) == 6 for ids in candidate_sets)
    assert candidate_sets[0].isdisjoint(candidate_sets[1])

    quality_path = root / "business-quality.json"
    quality_cli = _invoke([
        "assess-business-quality", "--l4-run", str(served.run_root), "--l3-root", str(l3),
        "--output", str(quality_path),
    ])
    quality = json.loads(quality_path.read_text())
    assert quality["summary"]["blocker_count"] == 0

    before_trace = _snapshot(windows, l1, domain, l2, l3, l4)
    serving_entities = {row["entity_id"]: row for row in rows.semantic_asserted_entities}
    source_payloads = {row["source_identity"]["source_unit_id"]: row for row in payloads}
    traces = []
    for candidate in validated.candidate_results:
        proposed = _trace(candidate.candidate_id, l1, domain, l2)
        full = _trace(candidate.semantic_id, l1, domain, l2, l4=served.run_root, l3=l3)
        assert proposed["authority"]["schema_state"] == full["authority"]["schema_state"] == "frozen"
        assert proposed["validation_state"] == "L2_proposals_only" and not proposed["validation"]
        assert full["authority"]["domain_contract_hash"] == frozen_hash
        assert full["validation"] and full["serving_records"]
        assert full["semantic_accuracy"] == "not_assessed"
        assert all(row["evidence_span_ids"] for row in full["validation"])
        assert len(full["observations"]) == 1
        observation = full["observations"][0]
        traced = observation["candidate"]
        assert traced["candidate_kind"] == candidate.candidate_kind
        assert observation["source"]["original_byte_hash"]
        assert "text" not in observation["source"]
        payload = source_payloads[traced["source_unit_id"]]
        anchor, offset = traced["proposed_anchor"], payload["source_identity"]["slice_start"]
        assert payload["source_text"][anchor["span_start"] - offset:anchor["span_end"] - offset] == anchor["quote"]
        serving = full["serving_records"][0]["record"]
        assert traced["candidate_id"] in serving["candidate_ids"]
        assert serving["evidence_span_ids"] == full["validation"][0]["evidence_span_ids"]
        if candidate.candidate_kind == "property":
            owner_id = traced["proposed_owner_entity_id"]
            assert serving["entity_id"] == owner_id
            assert serving["semantic_property_id"] == traced["approved_semantic_id"]
            assert serving["normalized_value_json"] == traced["normalized_value_json"]
            assert serving_entities[owner_id]["label"] in anchor["quote"]
            assert str(json.loads(serving["normalized_value_json"])) in anchor["quote"]
        elif candidate.candidate_kind == "relationship":
            assert serving["source_entity_id"] == traced["proposed_source_entity_id"]
            assert serving["target_entity_id"] == traced["proposed_target_entity_id"]
            assert serving["semantic_relationship_id"] == traced["approved_semantic_id"]
            for end in ("source", "target"):
                entity = serving_entities[serving[f"{end}_entity_id"]]
                assert entity["most_specific_type_id"] == traced[f"proposed_{end}_semantic_type_id"]
                assert entity["label"] in anchor["quote"]
        else:
            assert serving["label"] == traced["proposed_label"]
            assert serving["label"] in anchor["quote"]
        assert full == _trace(candidate.semantic_id, l1, domain, l2, l4=served.run_root, l3=l3)
        traces.append(full)
    assert before_trace == _snapshot(windows, l1, domain, l2, l3, l4)
    assert len({trace["observations"][0]["source"]["relative_source_ref"] for trace in traces}) == 2
    assert {trace["observations"][0]["candidate"]["candidate_kind"] for trace in traces} == {
        "entity", "property", "relationship",
    }
    assert not network_attempts
    assert source_before == _snapshot(source, prepared_path, intake)

    authority = json.loads((l2 / "approved-reextraction-authority.json").read_text())
    input_budget = extracted["input_budget"]
    assert input_budget["checked_requests"] == 2
    assert input_budget["minimum_remaining_context_tokens"] >= 0
    from fabric_kg_builder.enrichment.approved_input_budget import account_request

    actual_accounting = [account_request(request, authority["input_budget"]) for request in requests]
    assert all(row["remaining_context_tokens"] >= 0 for row in actual_accounting)
    from fabric_kg_builder.enrichment.approved_few_shot import render_system_prompt

    _, examples = render_system_prompt(core.SYSTEM_PROMPT, contract)
    assert len(canonical_json(examples).encode()) < 8000
    report = {
        "mode": "Two local sample-document OFFLINE deterministic smoke test and regression",
        "discovery_mode": discovery_mode,
        "report_path": str((root / "report.json").resolve()),
        "semantic_accuracy": "not assessed; injected responses do not measure GPT quality or Surface product accuracy",
        "sample_documents": sorted(path.name for path in source.glob("*.html")),
        "schema": {
            "frozen_contract_hash": frozen_hash, "unchanged_after_extraction_and_trace": True,
            "working_schema_runs": 1, "windows": 2, "final_approved_schemas": 1,
            "entity_types": len(contract.candidate_model.entity_types),
            "relationship_types": len(contract.candidate_model.relationship_types),
            "declared_properties": sum(len(row.declared_properties) for row in contract.candidate_model.entity_types),
        },
        "coverage": {"documents_prepared": 2, "documents_discovered": 2, "documents_reextracted": 2,
                     "documents_traced": 2, "candidate_ids_disjoint_across_documents": True},
        "results": {
            "asserted_entities": len(rows.semantic_asserted_entities),
            "asserted_relationships": len(rows.semantic_asserted_relationships),
            "asserted_properties": len(rows.semantic_asserted_properties),
            "candidate_lifecycle_counts": dict(Counter(row.current_state for row in validated.candidate_results)),
            "evidence_backed_candidates": sum(bool(row.evidence_span_ids) for row in validated.candidate_results),
            "unresolved_candidates": sum(row.current_state != "asserted" for row in validated.candidate_results),
            "numeric_values": [80.5, 81.5],
        },
        "business_quality": quality["summary"],
        "data_lineage": extracted["data_lineage"],
        "input_budget": input_budget,
        "actual_sdk_request_accounting": actual_accounting,
        "few_shot": {"hash": authority["few_shot_hash"], "serialized_utf8_bytes": len(canonical_json(examples).encode()),
                     "rendered_system_utf8_bytes": len(authority["system_prompt"].encode())},
        "trace": {"asserted_records": len(traces), "proposed_records": len(traces),
                  "kinds": ["entity", "property", "relationship"], "read_only": True,
                  "stable_repeat": True, "before_freeze_rejected": True,
                  "exact_quotes_and_field_owners_and_relationship_endpoints_verified": True},
        "calls": {
            "discovery_design_mock_logical_calls": len(model.offline_requests),
            "schema_discovery_sdk_stub_physical_attempts": len(model.document_requests) if whole_document else 0,
            "approved_extraction_mock_logical_calls": len(requests),
            "sdk_stub_physical_attempts": extracted["physical_calls"],
            "external_network_calls": 0, "ocr_calls": 0, "reextraction_original_source_reads": 0,
            "resume_model_calls": 0,
        },
        "quality_cli": quality_cli,
        "artifacts": {
            "sample_documents": "source/",
            "few_shot_examples": "few-shot-examples.json",
            "deterministic_response_examples": "response-examples.json",
            "persisted_extraction_responses": "fresh-l2/reextraction-responses/",
            "business_quality": "business-quality.json",
            "lineage_traces": "lineage-traces.json",
        },
        "reproduce": (
            "TIKTOKEN_CACHE_DIR=<populated-persistent-tokenizer-cache> "
            "FKG_OFFLINE_REGRESSION_ROOT=<new-persistent-directory> "
            ".venv/bin/pytest --no-cov -s tests/unit/test_schema2_two_documents.py"
        ),
    }
    if whole_document:
        report["generalized_schema"] = {
            "prompt_version": working.config.prompt_version,
            "model": "gpt-5.4", "model_version": "2026-03-05",
            "provider_model": "gpt-5.4-2026-03-05",
            "capability_profile": model.capability_profile,
            "complete_document_calls": 2, "serial_windows": 2,
            "stable_concept_ids": sorted(concepts),
            "second_document_action": "update_concept",
            "layers": ["common", "domain"], "instance_candidates": 0,
            "accepted_schema_layers": expected_layers,
            "accepted_layers_preserved_in_design_context": True,
            "zero_call_instance_replay_rejected": True,
            "source_snapshot_immutable": True,
        }
        (root / "schema-discovery-requests.json").write_text(
            canonical_json(model.document_requests) + "\n", encoding="utf-8")
        (root / "schema-discovery-responses.json").write_text(
            canonical_json(model.document_responses) + "\n", encoding="utf-8")
        report["artifacts"].update({
            "schema_discovery_requests": "schema-discovery-requests.json",
            "schema_discovery_responses": "schema-discovery-responses.json",
        })
    (root / "few-shot-examples.json").write_text(canonical_json(examples) + "\n", encoding="utf-8")
    (root / "response-examples.json").write_text(
        canonical_json([_approved_response(request) for request in requests]) + "\n", encoding="utf-8",
    )
    (root / "lineage-traces.json").write_text(canonical_json(traces) + "\n", encoding="utf-8")
    (root / "report.json").write_text(canonical_json(report) + "\n", encoding="utf-8")
    assert json.loads((root / "report.json").read_text()) == report
    return report


@pytest.mark.parametrize("discovery_mode", ["chunked", "whole-document"])
def test_two_document_offline_frozen_schema_end_to_end(monkeypatch, discovery_mode):
    base = Path(os.environ.get(
        "FKG_OFFLINE_REGRESSION_ROOT",
        str(Path.cwd() / ".fkg" / "offline-regressions" / "two-documents"),
    )) / discovery_mode
    root = base if not base.exists() else base.with_name(f"{base.name}-{uuid4().hex[:8]}")
    report = run_two_document_regression(root, monkeypatch, discovery_mode=discovery_mode)
    print(f"OFFLINE {discovery_mode} regression report: {report['report_path']}")
