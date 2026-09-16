"""Review and repair a successful returned-ID create without changing ownership."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.deploy import schema2_prototype as p
from fabric_kg_builder.deploy import schema2_prototype_reconcile as r
from tests.unit.test_l5a_structured_publication import _inputs
from tests.unit.test_schema2_prototype_reconcile import FakeFabric, _Response, WORKSPACE


class ReturnedFabric(FakeFabric):
    lost_create = False
    graph_id = None
    stop_once = True
    async_create = True

    def request(self, method, url, **kwargs):
        operation_url = f"{p.API}/operations/{self.operation_id}"
        if method == "GET" and url in (operation_url, operation_url + "/result"):
            self.calls.append((method, url, None))
            return _Response(
                {"status": self.operation_status} if url == operation_url
                else self.items[self.graph_id],
            )
        if self.graph_id and url.endswith(f"/{self.graph_id}/getDefinition") and self.stop_once:
            self.stop_once = False
            raise p.PrototypePublicationError("Simulated old runtime readback failure")
        response = super().request(method, url, **kwargs)
        if method == "POST" and url.endswith("/graphModels"):
            self.graph_id = response.body["id"]
            if not self.async_create:
                return response
            return _Response(None, 202, {
                "x-ms-operation-id": self.operation_id, "Location": operation_url,
            })
        return response


@pytest.fixture
def owned(tmp_path, monkeypatch, request):
    source = _inputs(tmp_path / "sealed")["source"]
    l3 = tmp_path / "sealed" / ".fkg" / "l3"
    next(l3.rglob("output-manifest.json")).write_text(
        canonical_json(source.input_manifest.model_dump(mode="json")) + "\n",
    )
    backend = ReturnedFabric()
    backend.async_create = getattr(request, "param", "async") == "async"
    monkeypatch.setattr(p._Run, "request", lambda self, method, url, **kw: backend.request(method, url, **kw))
    import azure.identity
    import deltalake

    monkeypatch.setattr(azure.identity, "AzureCliCredential", lambda: SimpleNamespace(
        get_token=lambda *args: SimpleNamespace(token="offline"),
    ))

    def write(path, table, **kwargs):
        assert path not in backend.tables, "duplicate Delta write"
        backend.tables[path] = {"table": table, "history": kwargs["commit_properties"].custom_metadata}

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
    verified = []
    monkeypatch.setattr(p._Run, "graph_counts", lambda *a, **kw: verified.append("counts"))
    monkeypatch.setattr(p._Run, "graph_content", lambda *a, **kw: verified.append("content"))
    monkeypatch.setattr(p._Run, "companion_readiness", lambda *a, **kw: True)
    monkeypatch.setattr(p, "_compiler_hash", lambda: "original-runtime")
    kwargs = {
        "l4_run": source.root, "l3_root": l3, "workspace_id": WORKSPACE,
        "name_prefix": "owned", "plan_path": tmp_path / "plan.json",
        "journal_path": tmp_path / "journal.json", "materialize_dir": tmp_path / "artifacts",
        "approved_limitations": (p.METADATA_ONLY_ALIASES_LIMITATION,),
    }
    plan = p.publish_schema2_prototype(**kwargs, dry_run=True, approve_live=None)
    with pytest.raises(p.PrototypePublicationError, match="old runtime"):
        p.publish_schema2_prototype(**kwargs, dry_run=False, approve_live=plan["plan_hash"])
    journal = p._read_json(kwargs["journal_path"])
    assert journal["actions"]["create:graph"]["status"] == "identity-verified"
    monkeypatch.setattr(p, "_compiler_hash", lambda: "repaired-runtime")
    recovery = {
        "plan_path": kwargs["plan_path"], "journal_path": kwargs["journal_path"],
        "materialize": kwargs["materialize_dir"], "l4_run": source.root, "l3_root": l3,
        "kind": "graph", "item_id": backend.graph_id, "review_path": tmp_path / "review.json",
        "returned_id_runtime_repair": True,
    }
    backend.calls.clear()
    return SimpleNamespace(
        kwargs=kwargs, recovery=recovery, backend=backend, plan=plan, journal=journal, verified=verified,
    )


def _accept(case):
    preview = r.reconcile_prototype_create(**case.recovery)
    return r.reconcile_prototype_create(
        **case.recovery, accept_review=preview["review_hash"],
        actor="operator", rationale="Reviewed successful create and readback-only runtime repair",
    )


def _assert_reads_only(case):
    assert all(method == "GET" or method == "POST" and url.endswith("/getDefinition")
               for method, url, _ in case.backend.calls)


def test_public_cli_returned_id_review_accept_dry_run_resume(owned, monkeypatch):
    case = owned
    original_plan = case.kwargs["plan_path"].read_bytes()
    original_journal = case.kwargs["journal_path"].read_bytes()
    original_artifacts = r._artifact_digests(case.recovery["materialize"])
    flags = ["app", "reconcile-prototype-create", "--returned-id-runtime-repair"]
    aliases = {"plan_path": "plan", "journal_path": "prototype-journal", "review_path": "review"}
    for key, value in case.recovery.items():
        if key != "returned_id_runtime_repair":
            flags += ["--" + aliases.get(key, key.replace("_", "-")), str(value)]
    runner = CliRunner()
    preview = runner.invoke(cli, flags)
    assert preview.exit_code == 0, preview.output
    review = json.loads(preview.output)
    assert review["review"]["policy"] == r.RETURNED_ID_POLICY
    assert review["review"]["proof"]["native_definition"]
    assert review["review"]["proof"]["create_result"]["id"] == case.backend.graph_id
    assert case.kwargs["journal_path"].read_bytes() == original_journal
    accepted = runner.invoke(cli, flags + [
        "--accept-review", review["review_hash"], "--actor", "operator",
        "--rationale", "Successful returned Graph create, unchanged templates",
    ])
    assert accepted.exit_code == 0, accepted.output
    journal = p._read_json(case.kwargs["journal_path"])
    assert journal["actions"] == case.journal["actions"]
    assert {key: value for key, value in journal.items() if key != "runtime_repair"} == case.journal
    assert r._artifact_digests(case.recovery["materialize"]) == original_artifacts
    _assert_reads_only(case)

    def no_rematerialization(*args):
        raise AssertionError("Returned-ID resume must not rebuild sealed artifacts")

    monkeypatch.setattr(p, "_materialize", no_rematerialization)
    publish = [
        "app", "publish-structured", "--prototype-create-only",
        "--l4-run", str(case.kwargs["l4_run"]), "--l3-root", str(case.kwargs["l3_root"]),
        "--workspace-id", WORKSPACE, "--name-prefix", "owned",
        "--plan", str(case.kwargs["plan_path"]), "--prototype-journal", str(case.kwargs["journal_path"]),
        "--materialize", str(case.kwargs["materialize_dir"]),
        "--prototype-approve-limitation", p.METADATA_ONLY_ALIASES_LIMITATION,
    ]
    dry = runner.invoke(cli, publish + ["--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert p._read_json(case.kwargs["journal_path"]) == journal
    for _ in range(2):
        resumed = runner.invoke(cli, publish + ["--live", "--approve-live", case.plan["plan_hash"]])
        assert resumed.exit_code == 0, resumed.output
    assert case.verified == ["counts", "content", "counts", "content"]
    assert case.kwargs["plan_path"].read_bytes() == original_plan
    final = p._read_json(case.kwargs["journal_path"])
    action = final["actions"]["create:graph"]
    assert action["returned_item_id"] == action["item_id"] == case.backend.graph_id
    assert "ownership" not in action and "original_create_evidence" not in action
    assert action["definition_readback_hash"] == review["review"]["proof"]["definition_hash"]
    _assert_reads_only(case)


@pytest.mark.parametrize("mutation", [
    "ambiguous", "failed", "created", "operator-owned", "item-id", "returned-id", "missing-returned-id",
    "http", "operation-state", "operation-id", "operation-headers", "body", "response-error",
    "metadata-name", "metadata-workspace", "metadata-description", "metadata-id",
    "live-name", "live-operation", "live-result", "native-definition", "request",
    "wrong-kind", "existing-receipt",
])
def test_returned_repair_rejects_invalid_ownership_provenance_or_live_proof(owned, mutation):
    case = owned
    journal = p._read_json(case.kwargs["journal_path"])
    action = journal["actions"]["create:graph"]
    if mutation in ("ambiguous", "failed", "created"):
        action["status"] = {"ambiguous": "intent", "failed": "failed-retained", "created": "created"}[mutation]
    elif mutation == "operator-owned":
        action["ownership"] = "operator-reconciled"
    elif mutation in ("item-id", "returned-id"):
        action[mutation.replace("-", "_").replace("returned_id", "returned_item_id")] = WORKSPACE
    elif mutation == "missing-returned-id":
        del action["returned_item_id"]
    elif mutation == "http":
        action["http_status"] = 500
    elif mutation == "operation-state":
        action["operation_state"] = "Running"
    elif mutation == "operation-id":
        action["operation_id"] = WORKSPACE
    elif mutation == "operation-headers":
        action["response_headers"] = {}
    elif mutation == "body":
        action["response_body"] = {"id": WORKSPACE}
    elif mutation == "response-error":
        action["response_body_error"] = "invalid-json"
    elif mutation.startswith("metadata-"):
        key = {"name": "displayName", "workspace": "workspaceId", "description": "description", "id": "id"}[mutation[9:]]
        action["metadata"][key] = "different"
    elif mutation == "live-name":
        case.backend.items[case.backend.graph_id]["displayName"] = "different"
    elif mutation == "live-operation":
        case.backend.operation_status = "Failed"
    elif mutation == "live-result":
        case.backend.items[case.backend.graph_id]["id"] = WORKSPACE
    elif mutation == "native-definition":
        case.backend.definitions[case.backend.graph_id]["parts"].pop()
    elif mutation == "request":
        action["request_hash"] = "different"
    elif mutation == "wrong-kind":
        case.recovery["kind"] = "ontology"
    else:
        journal["runtime_repair"] = {"policy": r.POLICY}
    p._atomic_json(case.kwargs["journal_path"], journal)
    before = case.kwargs["journal_path"].read_bytes()
    with pytest.raises(p.PrototypePublicationError):
        r.reconcile_prototype_create(**case.recovery)
    assert case.kwargs["journal_path"].read_bytes() == before
    assert not case.recovery["review_path"].exists()
    _assert_reads_only(case)


@pytest.mark.parametrize("mutation", ["plan-bytes", "journal-bytes", "template-bytes", "compiler", "native"])
def test_acceptance_rechecks_exact_review_and_does_not_write_on_drift(owned, monkeypatch, mutation):
    case = owned
    preview = r.reconcile_prototype_create(**case.recovery)
    if mutation == "plan-bytes":
        path = case.kwargs["plan_path"]
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "journal-bytes":
        path = case.kwargs["journal_path"]
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "template-bytes":
        path = case.recovery["materialize"] / "native-templates" / "graph.json"
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "compiler":
        monkeypatch.setattr(p, "_compiler_hash", lambda: "unreviewed-runtime")
    else:
        case.backend.definitions[case.backend.graph_id]["parts"].pop()
    before = case.kwargs["journal_path"].read_bytes()
    with pytest.raises(p.PrototypePublicationError):
        r.reconcile_prototype_create(
            **case.recovery, accept_review=preview["review_hash"], actor="operator", rationale="reviewed",
        )
    assert case.kwargs["journal_path"].read_bytes() == before
    _assert_reads_only(case)


@pytest.mark.parametrize("mutation", [
    "plan-bytes", "template-bytes", "missing-table", "extra-template", "bound-bytes",
    "returned-id", "response-header", "new-action-field", "compiler", "native", "operation",
    "baseline",
    "missing-null-evidence",
])
def test_resume_revalidates_receipt_artifacts_and_live_proof_before_mutation(owned, monkeypatch, mutation):
    case = owned
    _accept(case)
    if mutation == "plan-bytes":
        path = case.kwargs["plan_path"]
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation in ("template-bytes", "bound-bytes"):
        directory = "native-templates" if mutation == "template-bytes" else "native-bound"
        path = case.recovery["materialize"] / directory / "graph.json"
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "missing-table":
        next((case.recovery["materialize"] / "tables").glob("*.parquet")).unlink()
    elif mutation == "extra-template":
        (case.recovery["materialize"] / "native-templates" / "extra.json").write_text("{}")
    elif mutation in ("returned-id", "response-header", "new-action-field", "baseline", "missing-null-evidence"):
        journal = p._read_json(case.kwargs["journal_path"])
        action = journal["actions"]["create:graph"]
        if mutation == "returned-id":
            action["returned_item_id"] = WORKSPACE
        elif mutation == "response-header":
            action["response_headers"]["RequestId"] = "changed"
        elif mutation == "new-action-field":
            action["unexpected"] = True
        elif mutation == "baseline":
            journal["baseline_item_ids"].append(WORKSPACE)
        else:
            del action["response_body"]
        p._atomic_json(case.kwargs["journal_path"], journal)
    elif mutation == "compiler":
        monkeypatch.setattr(p, "_compiler_hash", lambda: "unreviewed-runtime")
    elif mutation == "native":
        case.backend.definitions[case.backend.graph_id]["parts"].pop()
    else:
        case.backend.operation_status = "Failed"
    before = case.kwargs["journal_path"].read_bytes()
    with pytest.raises(p.PrototypePublicationError):
        p.publish_schema2_prototype(**case.kwargs, dry_run=False, approve_live=case.plan["plan_hash"])
    assert case.kwargs["journal_path"].read_bytes() == before
    assert not case.verified
    _assert_reads_only(case)


def test_existing_reconciliation_still_refuses_returned_owned_graph(owned):
    with pytest.raises(p.PrototypePublicationError, match="without an item ID"):
        r.reconcile_prototype_create(**{**owned.recovery, "returned_id_runtime_repair": False})
    assert not owned.backend.calls


def test_receipt_cannot_be_replaced(owned):
    _accept(owned)
    before = owned.kwargs["journal_path"].read_bytes()
    with pytest.raises(p.PrototypePublicationError, match="cannot be replaced"):
        r.reconcile_prototype_create(**owned.recovery)
    assert owned.kwargs["journal_path"].read_bytes() == before


@pytest.mark.parametrize("owned", ["sync"], indirect=True)
def test_synchronous_returned_create_retains_response_and_ownership(owned):
    action = owned.journal["actions"]["create:graph"]
    assert action["http_status"] == 201
    assert action["response_body"]["id"] == owned.backend.graph_id
    _accept(owned)
    result = p.publish_schema2_prototype(**owned.kwargs, dry_run=False, approve_live=owned.plan["plan_hash"])
    assert result["actions"]["create:graph"]["response_body"] == action["response_body"]
    assert "ownership" not in result["actions"]["create:graph"]
    _assert_reads_only(owned)


def test_live_runtime_change_requires_explicit_accepted_review(owned):
    before = owned.kwargs["journal_path"].read_bytes()
    with pytest.raises(p.PrototypePublicationError, match="accepted operator"):
        p.publish_schema2_prototype(**owned.kwargs, dry_run=False, approve_live=owned.plan["plan_hash"])
    assert owned.kwargs["journal_path"].read_bytes() == before
    assert not owned.backend.calls
