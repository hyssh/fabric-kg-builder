"""Offline public recovery of a server-created item whose response was lost."""

import copy
import base64
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.deploy import schema2_prototype as p
from fabric_kg_builder.deploy import schema2_prototype_reconcile as r
from tests.unit.test_l5a_structured_publication import _inputs
from tests.unit.test_schema2_prototype_agent import _Backend, _Response, WORKSPACE


class FakeFabric(_Backend):
    lost_create = True

    def request(self, method, url, **kwargs):
        result = super().request(method, url, **kwargs)
        if method == "POST" and url.rsplit("/", 1)[-1] in p.COLLECTIONS.values():
            item_id = result.body["id"]
            self.items[item_id].update({
                "workspaceId": WORKSPACE, "description": kwargs["json"]["description"],
            })
            if url.endswith("/ontologies") and self.lost_create:
                self.lost_create = False
                raise AttributeError("Simulated old receiver: null body has no get")
            return _Response(self.items[item_id], 201)
        return result


@pytest.fixture
def paused(tmp_path, monkeypatch):
    source = _inputs(tmp_path / "sealed")["source"]
    l3 = tmp_path / "sealed" / ".fkg" / "l3"
    next(l3.rglob("output-manifest.json")).write_text(
        canonical_json(source.input_manifest.model_dump(mode="json")) + "\n",
    )
    backend = FakeFabric()
    monkeypatch.setattr(p._Run, "request", lambda self, method, url, **kw: backend.request(method, url, **kw))
    import azure.identity
    import deltalake

    monkeypatch.setattr(azure.identity, "AzureCliCredential", lambda: SimpleNamespace(
        get_token=lambda *args: SimpleNamespace(token="offline"),
    ))
    writes = []

    def write(path, table, **kwargs):
        assert path not in backend.tables, "duplicate Delta write"
        writes.append(path)
        backend.tables[path] = {
            "table": table, "history": kwargs["commit_properties"].custom_metadata,
        }

    class Delta:
        def __init__(self, path, **kwargs):
            self.entry = backend.tables[path]

        def history(self, count):
            return [self.entry["history"]]

        def version(self):
            return 0

        def to_pyarrow_table(self):
            return self.entry["table"]

    monkeypatch.setattr(deltalake, "write_deltalake", write)
    monkeypatch.setattr(deltalake, "DeltaTable", Delta)
    monkeypatch.setattr(p._Run, "graph_counts", lambda *a, **kw: None)
    monkeypatch.setattr(p._Run, "graph_content", lambda *a, **kw: None)
    monkeypatch.setattr(p._Run, "companion_readiness", lambda *a, **kw: True)
    monkeypatch.setattr(p, "_compiler_hash", lambda: "old-runtime-hash")
    kwargs = {
        "l4_run": source.root, "l3_root": l3, "workspace_id": WORKSPACE,
        "name_prefix": "recovery", "plan_path": tmp_path / "plan.json",
        "journal_path": tmp_path / "journal.json", "materialize_dir": tmp_path / "artifacts",
    }
    plan = p.publish_schema2_prototype(**kwargs, dry_run=True, approve_live=None)
    with pytest.raises(p.PrototypePublicationError, match="retained"):
        p.publish_schema2_prototype(**kwargs, dry_run=False, approve_live=plan["plan_hash"])
    journal = p._read_json(kwargs["journal_path"])
    assert journal["actions"]["create:ontology"]["status"] == "intent"
    item_id = next(key for key, value in backend.items.items() if value["type"] == "Ontology")
    monkeypatch.setattr(p, "_compiler_hash", lambda: "repaired-runtime-hash")
    recovery = {
        "plan_path": kwargs["plan_path"], "journal_path": kwargs["journal_path"],
        "materialize": kwargs["materialize_dir"], "l4_run": source.root, "l3_root": l3,
        "kind": "ontology", "item_id": item_id, "review_path": tmp_path / "review.json",
    }
    return SimpleNamespace(
        kwargs=kwargs, recovery=recovery, backend=backend, plan=plan,
        journal=journal, writes=writes, item_id=item_id,
    )


def _accept(case):
    preview = r.reconcile_prototype_create(**case.recovery)
    return r.reconcile_prototype_create(
        **case.recovery, accept_review=preview["review_hash"],
        actor="operator@example.test", rationale="Observed exact run-scoped server creation after lost response",
    )


def test_public_cli_preview_accept_and_original_plan_resume(paused):
    case = paused
    plan_bytes = case.kwargs["plan_path"].read_bytes()
    journal_bytes = case.kwargs["journal_path"].read_bytes()
    artifacts = {
        str(path): path.read_bytes() for path in case.kwargs["materialize_dir"].rglob("*") if path.is_file()
    }
    flags = ["app", "reconcile-prototype-create"]
    aliases = {"plan_path": "plan", "journal_path": "prototype-journal", "review_path": "review"}
    for key, value in case.recovery.items():
        flags += ["--" + aliases.get(key, key.replace("_", "-")), str(value)]
    runner = CliRunner()
    preview = runner.invoke(cli, flags)
    assert preview.exit_code == 0, preview.output
    review = json.loads(preview.output)
    assert review["review"]["authority"] == "review-only-not-authorizing"
    assert case.kwargs["journal_path"].read_bytes() == journal_bytes
    assert case.kwargs["plan_path"].read_bytes() == plan_bytes
    with pytest.raises(p.PrototypePublicationError, match="accepted operator"):
        p.publish_schema2_prototype(**case.kwargs, dry_run=False, approve_live=case.plan["plan_hash"])
    accepted = runner.invoke(cli, flags + [
        "--accept-review", review["review_hash"], "--actor", "operator",
        "--rationale", "Exact server-created item and run/source proof independently reviewed",
    ])
    assert accepted.exit_code == 0, accepted.output
    journal = p._read_json(case.kwargs["journal_path"])
    action = journal["actions"]["create:ontology"]
    assert action["status"] == "intent"
    assert "http_status" not in action and "returned_item_id" not in action
    assert action["original_create_evidence"] == case.journal["actions"]["create:ontology"]
    assert action["ownership"] == "operator-reconciled"
    assert case.kwargs["plan_path"].read_bytes() == plan_bytes
    assert all(Path(path).read_bytes() == data for path, data in artifacts.items())
    write_count = len(case.writes)
    result = p.publish_schema2_prototype(**case.kwargs, dry_run=False, approve_live=case.plan["plan_hash"])
    assert result["status"] == "structural-verification-complete"
    assert result["plan_hash"] == case.plan["plan_hash"]
    assert result["item_ids"]["ontology"] == case.item_id
    assert len(case.writes) == write_count
    creates = [url for method, url, body in case.backend.calls if method == "POST" and body and "displayName" in body]
    assert sum(url.endswith("/ontologies") for url in creates) == 1
    assert sum(url.endswith("/graphModels") for url in creates) == 1
    assert case.kwargs["plan_path"].read_bytes() == plan_bytes
    observed = next(item for item in result["observed_new_items"] if item["metadata"]["id"] == case.item_id)
    assert observed["ownership"] == "operator-reconciled"
    assert result["actions"]["create:ontology"]["status"] == "intent"


@pytest.mark.parametrize("mutation,match", [
    ("description", "description"), ("workspace", "workspace"), ("name", "name"),
    ("type", "type"), ("id", "ID"), ("definition", "definition"),
    ("baseline", "baseline"), ("request", "request hash"),
    ("compiler-semantic", "semantic plan"), ("plan-hash", "plan"),
    ("materialized", "materialized"), ("duplicate", "collision"),
])
def test_reconciliation_rejects_wrong_identity_source_or_proof(paused, monkeypatch, mutation, match):
    case = paused
    item = case.backend.items[case.item_id]
    if mutation in ("description", "workspace", "name", "type", "id"):
        field = {"workspace": "workspaceId", "name": "displayName"}.get(mutation, mutation)
        item[field] = str(uuid.uuid4())
    elif mutation == "definition":
        case.backend.definitions[case.item_id]["parts"].pop()
    elif mutation in ("baseline", "request"):
        journal = p._read_json(case.kwargs["journal_path"])
        if mutation == "baseline":
            journal["baseline_item_ids"].append(case.item_id)
        else:
            journal["actions"]["create:ontology"]["request_hash"] = "wrong"
        p._atomic_json(case.kwargs["journal_path"], journal)
    elif mutation == "compiler-semantic":
        original = p._compile

        def changed(*args):
            compilation = original(*args)
            compilation.provenance["new-authority"] = True
            return compilation

        monkeypatch.setattr(p, "_compile", changed)
    elif mutation == "plan-hash":
        plan = p._read_json(case.kwargs["plan_path"])
        plan["compiler_hash"] = "tampered"
        p._atomic_json(case.kwargs["plan_path"], plan)
    elif mutation == "materialized":
        path = case.kwargs["materialize_dir"] / "native-bound" / "ontology.json"
        p._atomic_json(path, {"parts": []})
    elif mutation == "duplicate":
        duplicate = copy.deepcopy(item)
        duplicate["id"] = str(uuid.uuid4())
        case.backend.items[duplicate["id"]] = duplicate
    before = case.kwargs["journal_path"].read_bytes()
    calls = len(case.backend.calls)
    with pytest.raises(p.PrototypePublicationError, match=match):
        r.reconcile_prototype_create(**case.recovery)
    assert case.kwargs["journal_path"].read_bytes() == before
    assert all(method == "GET" or url.endswith("/getDefinition") for method, url, _ in case.backend.calls[calls:])


@pytest.mark.parametrize("mutation", ["hash", "actor", "journal-race", "metadata-race", "receipt", "compiler"])
def test_acceptance_and_resume_fail_closed(paused, monkeypatch, mutation):
    case = paused
    preview = r.reconcile_prototype_create(**case.recovery)
    kwargs = dict(case.recovery, accept_review=preview["review_hash"], actor="operator", rationale="Reviewed")
    if mutation == "hash":
        kwargs["accept_review"] = "0" * 64
    elif mutation == "actor":
        kwargs["actor"] = " "
    elif mutation == "journal-race":
        journal = p._read_json(case.kwargs["journal_path"])
        journal["status"] = "changed"
        p._atomic_json(case.kwargs["journal_path"], journal)
    elif mutation == "metadata-race":
        case.backend.items[case.item_id]["description"] = "other run"
    else:
        r.reconcile_prototype_create(**kwargs)
        if mutation == "receipt":
            journal = p._read_json(case.kwargs["journal_path"])
            journal["runtime_repair"]["review"]["semantic_comparison_hash"] = "tampered"
            p._atomic_json(case.kwargs["journal_path"], journal)
        else:
            monkeypatch.setattr(p, "_compiler_hash", lambda: "unapproved-third-runtime")
        kwargs = None
    before = case.kwargs["journal_path"].read_bytes()
    with pytest.raises(p.PrototypePublicationError):
        if kwargs:
            r.reconcile_prototype_create(**kwargs)
        else:
            p.publish_schema2_prototype(**case.kwargs, dry_run=False, approve_live=case.plan["plan_hash"])
    assert case.kwargs["journal_path"].read_bytes() == before


@pytest.mark.parametrize("body", [None, [], "unexpected", 3])
def test_receiver_saves_headers_before_body_shape_and_resume_never_posts(tmp_path, body):
    item_id, operation_id = str(uuid.uuid4()), str(uuid.uuid4())
    plan = {
        "names": {"ontology": "test_ontology"}, "description": "run description",
        "workspace_id": WORKSPACE,
    }
    definition = {"parts": []}
    action = {
        "status": "intent", "kind": "ontology",
        "request_hash": canonical_sha256({"displayName": "test_ontology", "description": "run description", "definition": definition}),
    }
    run = object.__new__(p._Run)
    run.plan, run.path = plan, tmp_path / "journal.json"
    run.data = {"actions": {"create:ontology": action}}
    headers = {"x-ms-operation-id": operation_id, "Location": f"{p.API}/operations/{operation_id}"}
    response = _Response(body, 202, headers)
    original_json = response.json

    def parse():
        saved = p._read_json(run.path)["actions"]["create:ontology"]
        assert saved["http_status"] == 202 and saved["operation_id"] == operation_id
        return original_json()

    response.json = parse
    if body is None:
        assert run._receive(response, action) == {}
    else:
        with pytest.raises(p.PrototypePublicationError, match="body must"):
            run._receive(response, action)
    saved = p._read_json(run.path)["actions"]["create:ontology"]
    assert saved["response_body"] == body and saved["location"] == headers["Location"]
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url))
        assert method == "GET", "duplicate create"
        if url.endswith("/result"):
            return _Response({"id": item_id})
        if "/operations/" in url:
            return _Response({"status": "Succeeded"})
        return _Response({"id": item_id, "displayName": "test_ontology", "type": "Ontology"})

    run.request = request
    assert run.create("ontology", definition) == item_id
    assert run.create("ontology", definition) == item_id
    assert all(method == "GET" for method, _ in calls)


def test_invalid_json_retains_raw_response_and_operation(tmp_path):
    run = object.__new__(p._Run)
    run.path = tmp_path / "journal.json"
    action = {"status": "intent"}
    run.data = {"actions": {"create:ontology": action}}
    operation_id = str(uuid.uuid4())
    response = _Response(None, 202, {"x-ms-operation-id": operation_id})
    response.text = "not JSON"
    response.json = lambda: json.loads(response.text)
    with pytest.raises(p.PrototypePublicationError, match="Invalid Fabric JSON"):
        run._receive(response, action)
    saved = p._read_json(run.path)["actions"]["create:ontology"]
    assert saved["operation_id"] == operation_id
    assert saved["response_body_text"] == "not JSON"


def test_agent_explicitly_reports_reconciled_handoff_incompatibility(paused):
    from fabric_kg_builder.deploy import schema2_prototype_agent as agent

    _accept(paused)
    with pytest.raises(agent.Error, match="incompatible with operator-reconciled/runtime-repaired"):
        agent._handoff(
            prototype_plan=paused.kwargs["plan_path"], prototype_journal=paused.kwargs["journal_path"],
            materialize=paused.kwargs["materialize_dir"], l4_run=paused.kwargs["l4_run"],
            l3_root=paused.kwargs["l3_root"], workspace_id=WORKSPACE, name_prefix="recovery",
        )


@pytest.mark.parametrize("field", [
    "tables", "native_definition_templates", "provenance", "limitations",
    "approved_limitations", "graph_readback_policy", "names", "description",
    "graph_catalog", "policy", "late_binding", "dependency_order", "semantic_model",
])
def test_runtime_comparison_does_not_ignore_any_semantic_field(field):
    original = {"compiler_hash": "old", "plan_hash": "old-plan", field: {"proof": "old"}}
    candidate = copy.deepcopy(original)
    candidate.update(compiler_hash="new", plan_hash="new-plan")
    assert r._semantic(candidate) == r._semantic(original)
    candidate[field] = {"proof": "changed"}
    assert r._semantic(candidate) != r._semantic(original)


def test_fresh_readback_race_rejects_acceptance_without_writes(paused, monkeypatch):
    preview = r.reconcile_prototype_create(**paused.recovery)
    before = paused.kwargs["journal_path"].read_bytes()
    original = paused.backend.request
    definition_reads = 0

    def racing(method, url, **kwargs):
        nonlocal definition_reads
        if url.endswith("/getDefinition"):
            definition_reads += 1
            if definition_reads == 2:
                paused.backend.definitions[paused.item_id]["parts"].pop()
        return original(method, url, **kwargs)

    monkeypatch.setattr(paused.backend, "request", racing)
    with pytest.raises(p.PrototypePublicationError, match="definition"):
        r.reconcile_prototype_create(
            **paused.recovery, accept_review=preview["review_hash"], actor="operator", rationale="Reviewed",
        )
    assert paused.kwargs["journal_path"].read_bytes() == before


def test_resume_rechecks_definition_and_original_intent(paused):
    _accept(paused)
    paused.backend.definitions[paused.item_id]["parts"].pop()
    with pytest.raises(p.PrototypePublicationError, match="definition mismatch"):
        p.publish_schema2_prototype(**paused.kwargs, dry_run=False, approve_live=paused.plan["plan_hash"])
    assert not any(url.endswith("/graphModels") for method, url, _ in paused.backend.calls if method == "POST")
    journal = p._read_json(paused.kwargs["journal_path"])
    assert journal["actions"]["create:ontology"]["status"] == "intent"
    assert "returned_item_id" not in journal["actions"]["create:ontology"]


def _service_platform(plan):
    envelope = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {
            "type": "Ontology", "displayName": plan["names"]["ontology"],
            "description": plan["description"],
        },
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    }
    return {
        "path": ".platform", "payloadType": "InlineBase64",
        "payload": base64.b64encode(json.dumps(envelope).encode()).decode(),
    }


def test_service_platform_extra_and_json_formatting_are_reviewed_then_resumed(paused):
    parts = paused.backend.definitions[paused.item_id]["parts"]
    for part in parts:
        if part["path"].endswith(".json"):
            payload = json.loads(base64.b64decode(part["payload"]))
            part["payload"] = base64.b64encode(json.dumps(payload, indent=3).encode()).decode()
    parts.append(_service_platform(paused.plan))
    _accept(paused)
    receipt = p._read_json(paused.kwargs["journal_path"])["runtime_repair"]
    assert receipt["review"]["proof"]["service_platform_hash"]
    result = p.publish_schema2_prototype(**paused.kwargs, dry_run=False, approve_live=paused.plan["plan_hash"])
    assert result["item_ids"]["ontology"] == paused.item_id
    assert result["status"] == "structural-verification-complete"


@pytest.mark.parametrize("mutation", ["description", "displayName", "type", "duplicate", "shape", "encoding", "config", "schema"])
def test_service_platform_envelope_must_match_exact_plan(mutation):
    plan = {"names": {"ontology": "owned_run_ontology"}, "description": "exact unique run description"}
    part = _service_platform(plan)
    parts = [part]
    envelope = json.loads(base64.b64decode(part["payload"]))
    if mutation in ("description", "displayName", "type"):
        envelope["metadata"][mutation] = "foreign"
    elif mutation == "duplicate":
        parts.append(copy.deepcopy(part))
    elif mutation == "shape":
        envelope = None
    elif mutation == "encoding":
        part["payloadType"] = "External"
    elif mutation == "config":
        envelope["config"]["logicalId"] = "not-an-ID"
    elif mutation == "schema":
        envelope["$schema"] = "unreviewed-schema"
    part["payload"] = base64.b64encode(json.dumps(envelope).encode()).decode()
    with pytest.raises(p.PrototypePublicationError, match="platform"):
        r._platform_envelope({"parts": parts}, plan, "ontology")


def test_nonplatform_extra_definition_part_is_rejected(paused):
    paused.backend.definitions[paused.item_id]["parts"].append({
        "path": "unplanned-definition.json", "payloadType": "InlineBase64",
        "payload": base64.b64encode(b'{"unplanned":"material"}').decode(),
    })
    with pytest.raises(p.PrototypePublicationError, match="full native bound definition mismatch"):
        r.reconcile_prototype_create(**paused.recovery)
