"""Strict reviewed baseline and offline same-item repair regression."""

import base64
import copy
import hashlib
import json
import uuid

import pytest

from fabric_kg_builder.cli import cli
from fabric_kg_builder.deploy import current_definition_review as c
from fabric_kg_builder.deploy import schema2_ontology_presentation as m
from fabric_kg_builder.deploy import schema2_prototype as p
from fabric_kg_builder.deploy.fabric_ontology_definition import _part
from tests.unit.test_schema2_ontology_presentation import (
    _live, _plan, _reviewed_ontology, legacy_native_compilation, paused, repair,
)


def _changed(definition):
    current = copy.deepcopy(definition)
    changed_context = False
    for index, part in enumerate(current["parts"]):
        path = part["path"]
        payload = m._decode({"parts": [part]})[path]
        if path.startswith(("EntityTypes/", "RelationshipTypes/")) and path.endswith("/definition.json"):
            payload["name"] = "Existing_" + payload["id"]
            current["parts"][index] = _part(path, payload)
        if "/Contextualizations/" in path and not changed_context:
            payload["id"] = str(uuid.uuid4())
            path = path.rsplit("/", 1)[0] + "/" + payload["id"] + ".json"
            current["parts"][index] = _part(path, payload)
            changed_context = True
    assert changed_context
    return current


def _write_review(case, current):
    context = m._local(**{
        k: v for k, v in case.args.items() if k not in ("state", "current_definition_review")
    })
    definition_file = case.state.parent / "current-definition.json"
    definition_file.write_text(json.dumps(current))
    payload = {
        "version": "ontology-current-definition-review/1.0.0", "policy": c.POLICY,
        "workspace_id": case.args["workspace_id"], "ontology_id": case.item_id,
        "domain_contract_hash": context["evidence"]["publication_authority"]["domain_contract_hash"],
        "publication_plan_hash": context["plan"]["plan_hash"],
        "original_definition_hash": m._content_hash(context["original"]),
        "current_definition_hash": m._content_hash(current),
        "current_definition_file": str(definition_file),
        "current_definition_file_sha256": hashlib.sha256(definition_file.read_bytes()).hexdigest(),
        "actor": "Copilot test reviewer", "rationale": "Reviewed pre-existing names and local IDs only",
    }
    path = case.state.parent / "current-review.json"
    path.write_text(json.dumps(payload))
    case.args["current_definition_review"] = path
    return payload


def _setup(case):
    _reviewed_ontology(case)
    current = _changed(case.backend.definitions[case.item_id])
    case.backend.definitions[case.item_id] = current
    return current, _write_review(case, current)


def test_reviewed_current_baseline_retains_current_binding_bytes_and_resume(repair):
    original = copy.deepcopy(repair.backend.definitions[repair.item_id])
    frozen = {repair.args[k]: repair.args[k].read_bytes() for k in ("plan_path", "journal_path")}
    current, payload = _setup(repair)
    result = _plan(repair)
    plan = p._read_json(repair.state / "plan.json")
    evidence = plan["local_evidence"]["current_definition_review"]
    assert evidence["payload"] == payload
    assert base64.b64decode(evidence["raw_base64"]) == repair.args["current_definition_review"].read_bytes()
    assert base64.b64decode(evidence["definition_raw_base64"]) == json.dumps(current).encode()
    assert evidence["original_to_current_diff"] == c.validate_equivalence(original, current)["original_to_current_diff"]
    assert any(x.get("local_id_only") for x in evidence["original_to_current_diff"])
    backup = p._read_json(repair.state / "backup.json")
    replacement = p._read_json(repair.state / "replacement.json")
    assert backup["definition"] == current
    for part in current["parts"]:
        if "/Contextualizations/" in part["path"] or "/DataBindings/" in part["path"]:
            assert part in replacement["parts"]
    assert m._invariants(current) == m._invariants(replacement)
    assert all(x["pointer"] == "/name" for x in plan["allowed_field_diff"])
    receipt = _live(repair, result)
    assert receipt["publication_snapshot"]["original_definition_hash"] == m._content_hash(original)
    assert receipt["reviewed_current_baseline"]["current_definition_hash"] == m._content_hash(current)
    _live(repair, result, resume=True)
    assert len(repair.updates) == 1
    assert all(path.read_bytes() == data for path, data in frozen.items())


def test_default_still_rejects_preexisting_changes(repair):
    repair.backend.definitions[repair.item_id] = _changed(repair.backend.definitions[repair.item_id])
    with pytest.raises(ValueError, match="full native bound"):
        _plan(repair)
    assert not repair.updates


@pytest.mark.parametrize("field", [
    "workspace_id", "ontology_id", "domain_contract_hash", "publication_plan_hash",
    "original_definition_hash", "current_definition_hash", "current_definition_file_sha256",
    "policy", "actor", "rationale",
])
def test_review_wrong_authority_or_hash_rejected(repair, field):
    _, payload = _setup(repair)
    payload[field] = "" if field in ("actor", "rationale") else "0" * 64
    repair.args["current_definition_review"].write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        _plan(repair)
    assert not repair.updates


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("mutation", ["review-bytes", "review-body", "definition-bytes", "definition-data", "omit", "code"])
def test_review_drift_blocks_apply_and_resume(repair, monkeypatch, resume, mutation):
    _, payload = _setup(repair)
    result = _plan(repair)
    if resume:
        repair.mode = "lost-after"
        with pytest.raises(ValueError, match="Unknown update"):
            _live(repair, result)
    path = repair.args["current_definition_review"]
    if mutation == "review-bytes":
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "review-body":
        payload["actor"] = "Different reviewer"
        path.write_text(json.dumps(payload))
    elif mutation.startswith("definition-"):
        from pathlib import Path
        path = Path(payload["current_definition_file"])
        path.write_bytes(path.read_bytes() + (b"\n" if mutation == "definition-bytes" else b"broken"))
    elif mutation == "omit":
        repair.args.pop("current_definition_review")
    else:
        original_load = c.load_review

        def drift(*args, **kwargs):
            current, evidence = original_load(*args, **kwargs)
            evidence["code_sha256"] = "0" * 64
            return current, evidence

        monkeypatch.setattr(c, "load_review", drift)
    with pytest.raises(ValueError):
        _live(repair, result, resume=resume)
    assert len(repair.updates) == int(resume)


def test_current_remote_drift_before_apply_rejected(repair):
    current, _ = _setup(repair)
    result = _plan(repair)
    repair.backend.definitions[repair.item_id] = _changed(current)
    with pytest.raises(ValueError, match="explicit review"):
        _live(repair, result)
    assert not repair.updates


def test_current_drift_during_original_ownership_proof_rejected(repair, monkeypatch):
    current, _ = _setup(repair)
    original_definition = m._PresentationRun.definition
    reads = []

    def changing(self, kind, item_id):
        actual = original_definition(self, kind, item_id)
        reads.append(item_id)
        if len(reads) == 1:
            repair.backend.definitions[repair.item_id] = _changed(current)
        return actual

    monkeypatch.setattr(m._PresentationRun, "definition", changing)
    with pytest.raises(ValueError, match="full native bound"):
        _plan(repair)
    assert not repair.updates


def test_current_platform_and_regional_lro_plan_apply_resume(repair, monkeypatch):
    current, payload = _setup(repair)
    plan = p._read_json(repair.args["plan_path"])
    current["parts"].append(_part(".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Ontology", "displayName": plan["names"]["ontology"], "description": plan["description"]},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }))
    _write_review(repair, current)
    repair.mode = "202-null"
    repair.read_lro = True
    original_request = p._Run.request
    requests = []

    def regional(self, method, url, **kwargs):
        requests.append((method, url))
        response = original_request(self, method, url, **kwargs)
        if response.status_code == 202:
            response.headers["Location"] = (
                "https://wabi-west-europe-e-primary-redirect.analysis.windows.net"
                f"/v1/operations/{repair.operation_id}"
            )
        return response

    monkeypatch.setattr(p._Run, "request", regional)
    result = _plan(repair)
    _live(repair, result)
    _live(repair, result, resume=True)
    assert len(repair.updates) == 1
    assert current["parts"][-1] in repair.updates[0]["definition"]["parts"]
    assert any("/operations/" in url for _, url in requests)
    assert all(url.startswith(p.API + "/") for _, url in requests)


def test_review_does_not_relax_exact_reconciliation_metadata(repair):
    _setup(repair)
    repair.backend.items[repair.item_id]["sensitivityLabel"] = {"id": "changed"}
    with pytest.raises(ValueError, match="original ownership proof"):
        _plan(repair)
    assert not repair.updates


def test_reviewed_baseline_can_be_a_verified_previous_repair(repair):
    _setup(repair)
    first = _plan(repair)
    _live(repair, first)
    prior = repair.state
    frozen = {path: path.read_bytes() for path in prior.glob("*.json")}
    repair.state = prior.parent / "next-repair"
    repair.args.update(state=repair.state, previous_repair_state=prior)
    repair.args.pop("current_definition_review")
    _reviewed_ontology(repair, suffix="Next")
    second = _plan(repair)
    _live(repair, second)
    assert len(repair.updates) == 2
    assert all(path.read_bytes() == data for path, data in frozen.items())


@pytest.fixture
def native():
    ctx_id = "11111111-1111-4111-8111-111111111111"
    return {"parts": [
        _part("EntityTypes/1/definition.json", {
            "id": "1", "name": "Part", "baseEntityTypeId": "base", "entityIdParts": ["key"],
            "properties": [{"id": "key", "name": "id", "valueType": "String"}],
        }),
        _part("RelationshipTypes/2/definition.json", {
            "id": "2", "name": "Has", "source": {"entityTypeId": "1", "multiplicity": "Many"},
            "target": {"entityTypeId": "3", "multiplicity": "One"},
        }),
        _part(f"RelationshipTypes/2/Contextualizations/{ctx_id}.json", {
            "id": ctx_id,
            "dataBindingTable": {
                "itemId": "lake", "workspaceId": "workspace", "sourceSchema": "dbo",
                "sourceTableName": "edge_table", "sourceType": "LakehouseTable",
            },
            "sourceKey": {"sourceColumnName": "__source_entity_id", "targetPropertyId": "key"},
            "targetKey": {"sourceColumnName": "__target_entity_id", "targetPropertyId": "target"},
        }),
    ]}


@pytest.mark.parametrize("part_index,keys", [
    (0, ("id",)), (0, ("baseEntityTypeId",)), (0, ("entityIdParts", 0)),
    (0, ("properties", 0, "id")), (0, ("properties", 0, "name")), (0, ("properties", 0, "valueType")),
    (1, ("source", "entityTypeId")), (1, ("target", "entityTypeId")),
    (1, ("source", "multiplicity")), (1, ("target", "multiplicity")),
    (2, ("dataBindingTable", "itemId")), (2, ("dataBindingTable", "workspaceId")),
    (2, ("dataBindingTable", "sourceSchema")), (2, ("dataBindingTable", "sourceTableName")),
    (2, ("dataBindingTable", "sourceType")), (2, ("sourceKey", "sourceColumnName")),
    (2, ("sourceKey", "targetPropertyId")), (2, ("targetKey", "sourceColumnName")),
    (2, ("targetKey", "targetPropertyId")), (2, ("id",)),
])
def test_protected_changes_never_become_reviewable(native, part_index, keys):
    current = _changed(native)
    part = current["parts"][part_index]
    payload = m._decode({"parts": [part]})[part["path"]]
    value = payload
    for key in keys[:-1]:
        value = value[key]
    value[keys[-1]] = "changed"
    current["parts"][part_index] = _part(part["path"], payload)
    with pytest.raises(ValueError):
        c.validate_equivalence(native, current)


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate-path", "duplicate-binding", "cross-relationship", "order", "json-duplicate"])
def test_contextualization_scope_and_ambiguity_rejected(native, mutation):
    current = _changed(native)
    binding = copy.deepcopy(current["parts"][-1])
    if mutation == "missing":
        current["parts"].pop()
    elif mutation == "duplicate-path":
        current["parts"].append(binding)
    elif mutation in ("extra", "duplicate-binding"):
        payload = m._decode({"parts": [binding]})[binding["path"]]
        payload["id"] = str(uuid.uuid4())
        if mutation == "extra":
            payload["sourceKey"]["sourceColumnName"] = "other"
        current["parts"].append(_part(
            binding["path"].rsplit("/", 1)[0] + "/" + payload["id"] + ".json", payload,
        ))
    elif mutation == "cross-relationship":
        current["parts"][-1]["path"] = binding["path"].replace("RelationshipTypes/2/", "RelationshipTypes/4/")
    elif mutation == "order":
        current["parts"].reverse()
    else:
        raw = base64.b64decode(binding["payload"]).decode()
        current["parts"][-1]["payload"] = base64.b64encode(
            raw.replace('"sourceSchema": "dbo"', '"sourceSchema": "wrong", "sourceSchema": "dbo"').encode(),
        ).decode()
    with pytest.raises(ValueError):
        c.validate_equivalence(native, current)


@pytest.mark.parametrize("suffix", ["", "/result"])
def test_trusted_regional_operation_normalizes_to_canonical_origin(suffix):
    operation = str(uuid.uuid4())
    assert m._operation_url({
        "x-ms-operation-id": operation,
        "location": f"https://wabi-west-europe-e-primary-redirect.analysis.windows.net/v1/operations/{operation}{suffix}",
    }) == f"{p.API}/operations/{operation}"


@pytest.mark.parametrize("location", [
    "http://wabi-west-redirect.analysis.windows.net/v1/operations/{id}",
    "https://wabi-west-redirect.analysis.windows.net.evil.example/v1/operations/{id}",
    "https://wabi-west-redirect.analysis.windows.net:443/v1/operations/{id}",
    "https://user@wabi-west-redirect.analysis.windows.net/v1/operations/{id}",
    "https://wabi-west-redirect.analysis.windows.net/v1/operations/{id}?query=1",
    "https://wabi-west-redirect.analysis.windows.net/v1/operations/{id}#fragment",
    "https://wabi-west-redirect.analysis.windows.net/v1/operations/00000000-0000-0000-0000-000000000000",
    "https://wabi-west.analysis.windows.net/v1/operations/{id}",
])
def test_regional_poll_origin_and_operation_guards(location):
    operation = str(uuid.uuid4())
    with pytest.raises(ValueError):
        m._operation_url({"x-ms-operation-id": operation, "location": location.format(id=operation)})
    with pytest.raises(ValueError):
        m._operation_url({"location": location.format(id=operation)})
