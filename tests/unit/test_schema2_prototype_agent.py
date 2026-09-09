"""Offline, genuine sealed-source/publication-journal to native Data Agent path."""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.deploy import schema2_prototype as publication
from fabric_kg_builder.deploy import schema2_prototype_agent as agent
from fabric_kg_builder.knowledge.data_agent import (
    DataAgentSpec, DataSourceSpec, build_definition_parts, decode_stage_snapshot,
)
from fabric_kg_builder.knowledge.transport import HttpRequest
from tests.unit.test_l5a_structured_publication import _inputs

WORKSPACE = "9802a28a-fc89-48b8-b7ae-798d1cf2463f"
PREFIX = "kg20260908"


class _Response:
    def __init__(self, body, status=200, headers=None):
        self.body = copy.deepcopy(body)
        self.status_code = status
        self.headers = headers or {}
        self.content = b"json"

    def json(self):
        return copy.deepcopy(self.body)


class _Backend:
    def __init__(self):
        self.items = {}
        self.definitions = {}
        self.tables = {}
        self.counts = {}
        self.calls = []
        self.agent_creates = 0
        self.create_mode = "normal"
        self.operation_id = str(uuid.uuid4())
        self.operation_status = "Succeeded"
        self.operation_timeout_once = False
        self.agent_id = None
        self.definition_drift = False
        self.source_denied = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs.get("json"))))
        path = url.split("?", 1)[0]
        if path.endswith("/items") and method == "GET":
            return _Response({"value": list(self.items.values())})
        if "/operations/" in path:
            if self.operation_timeout_once:
                self.operation_timeout_once = False
                raise TimeoutError("fixture LRO read timed out")
            return _Response(
                {"id": self.agent_id} if path.endswith("/result")
                else {"status": self.operation_status}
            )
        if path.endswith("/executeQuery"):
            graph_id = path.split("/")[-2]
            key = "edges" if "[e]" in kwargs["json"]["query"] else "nodes"
            return _Response({"status": {"code": "00000"}, "result": {
                "data": [{"observed_count": self.counts[graph_id][key]}],
            }})
        if path.endswith("/getDefinition"):
            item_id = path.split("/")[-2]
            definition = copy.deepcopy(self.definitions[item_id])
            if self.definition_drift and item_id == self.agent_id:
                definition["parts"] = definition["parts"][:-1]
            return _Response({"definition": definition})
        collection = path.rsplit("/", 1)[-1]
        if method == "POST" and collection in {"lakehouses", "ontologies", "graphModels", "dataAgents"}:
            body = kwargs["json"]
            if collection == "dataAgents":
                self.agent_creates += 1
                if self.create_mode == "rejected":
                    return _Response({"errorCode": "FixtureDenied"}, 403)
            item_id = str(uuid.uuid4())
            metadata = {
                "id": item_id, "displayName": body["displayName"],
                "type": {"lakehouses": "Lakehouse", "ontologies": "Ontology",
                         "graphModels": "GraphModel", "dataAgents": "DataAgent"}[collection],
            }
            if collection == "lakehouses":
                metadata["properties"] = {"defaultSchema": "dbo", "sqlEndpointProperties": {
                    "id": str(uuid.uuid4()), "connectionString": "fixture.datawarehouse.fabric.microsoft.com",
                    "provisioningStatus": "Success",
                }}
            self.items[item_id] = metadata
            if "definition" in body:
                self.definitions[item_id] = body["definition"]
            if collection == "dataAgents":
                self.agent_id = item_id
                if self.create_mode == "timeout":
                    raise TimeoutError("fixture response lost after server create")
                if self.create_mode == "missing-id":
                    return _Response({}, 201)
                if self.create_mode == "lro":
                    return _Response({}, 202, {
                        "x-ms-operation-id": self.operation_id,
                        "Location": f"{publication.API}/operations/{self.operation_id}",
                    })
            return _Response(metadata, 201)
        item_id = collection
        if method == "GET" and item_id in self.items:
            if self.source_denied and item_id != self.agent_id:
                return _Response({"errorCode": "SourceReadDenied"}, 403)
            return _Response(self.items[item_id])
        raise AssertionError(f"Unexpected fixture HTTP operation: {method} {url}")


@pytest.fixture
def published(tmp_path, monkeypatch):
    source = _inputs(tmp_path / "sealed")["source"]
    # The existing helper returns its final sealed L3 manifest in memory.
    # Persist that exact manifest for the public path-based source reader.
    l3_root = tmp_path / "sealed" / ".fkg" / "l3"
    next(l3_root.rglob("output-manifest.json")).write_text(
        canonical_json(source.input_manifest.model_dump(mode="json")) + "\n",
    )
    backend = _Backend()
    original_run = publication._Run
    monkeypatch.setattr(
        original_run, "request",
        lambda _self, method, url, **kwargs: backend.request(method, url, **kwargs),
    )
    import azure.identity
    monkeypatch.setattr(azure.identity, "AzureCliCredential", lambda: SimpleNamespace(
        get_token=lambda *_args: SimpleNamespace(token="offline-fixture-token"),
    ))

    class PublishedRun(original_run):
        def delta(self, table_id, table, lakehouse_id):
            key = f"delta:{table_id}"
            path = f"abfss://{WORKSPACE}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}/Tables/dbo/{table_id}"
            proof = publication._table_proof(table)
            history = {
                "prototype_run_id": self.plan["run_id"],
                "prototype_plan_hash": self.plan["plan_hash"], "prototype_action": key,
            }
            self.data["actions"][key] = {
                "status": "verified", "path": path, "table_proof": proof, "delta_version": 0,
            }
            backend.tables[path] = {"table": table, "history": history, "version": 0}
            self.save()

        def graph_counts(self, graph_id, expected):
            backend.counts[graph_id] = expected
            self.data.setdefault("graph_count_readbacks", {})[graph_id] = {"expected": expected}
            self.save()

        def graph_content(self, graph_id, *_args, **_kwargs):
            self.data.setdefault("graph_content_readbacks", {})[graph_id] = {"status": "verified"}
            self.save()

        def companion_readiness(self, *, ontology_id, lakehouse_id, expected, **_kwargs):
            graph_id = str(uuid.uuid4())
            independent = self.data["actions"]["create:graph"]["item_id"]
            backend.definitions[graph_id] = copy.deepcopy(backend.definitions[independent])
            backend.items[graph_id] = {
                "id": graph_id, "displayName": "fixture-service-companion", "type": "GraphModel",
            }
            self.data["verified_service_companions"] = {graph_id: {
                "ontology_id": ontology_id, "lakehouse_id": lakehouse_id,
                "definition_readback_hash": canonical_sha256(
                    publication._definition_payloads(backend.definitions[graph_id])
                ),
            }}
            self.graph_counts(graph_id, expected)
            self.save()
            return True

    monkeypatch.setattr(publication, "_Run", PublishedRun)
    materialize = tmp_path / "materialized"
    plan_path, journal_path = tmp_path / "publication-plan.json", tmp_path / "publication-journal.json"
    publication_kwargs = {
        "l4_run": source.root, "l3_root": l3_root,
        "workspace_id": WORKSPACE, "name_prefix": PREFIX,
        "plan_path": plan_path, "materialize_dir": materialize, "journal_path": journal_path,
    }
    plan = publication.publish_schema2_prototype(**publication_kwargs, dry_run=True, approve_live=None)
    assert not plan["blockers"], plan["blockers"]
    publication.publish_schema2_prototype(
        **publication_kwargs, dry_run=False, approve_live=plan["plan_hash"],
    )
    # The service companion wire projection is independently covered by publisher
    # tests; this fixture substitutes only that external service representation.
    checks = publication._graph_readback_checks
    monkeypatch.setattr(publication, "_graph_readback_checks", lambda *args, **kwargs: (
        [] if kwargs.get("companion") else checks(*args, **kwargs)
    ))
    import deltalake

    class Delta:
        def __init__(self, path, **_kwargs):
            self.entry = backend.tables[path]

        def version(self):
            return self.entry["version"]

        def history(self, _count):
            return [self.entry["history"]]

        def to_pyarrow_dataset(self):
            import pyarrow.dataset as ds
            return ds.dataset(self.entry["table"])

    monkeypatch.setattr(deltalake, "DeltaTable", Delta)
    backend.calls.clear()
    return SimpleNamespace(
        backend=backend, source=source, publication_plan=plan,
        kwargs={
            "prototype_journal": journal_path, "prototype_plan": plan_path,
            "materialize": materialize, "l4_run": source.root,
            "l3_root": publication_kwargs["l3_root"], "workspace_id": WORKSPACE,
            "name_prefix": PREFIX, "out_state": tmp_path / "agent",
        },
    )


def _plan(published):
    return agent.publish_schema2_prototype_agent(**published.kwargs)


def _live(published, plan):
    return agent.publish_schema2_prototype_agent(
        **published.kwargs, live=True, approve_live=plan["plan_hash"], acknowledge_preview=True,
    )


def _cli_args(published):
    result = ["app", "publish-prototype-agent"]
    for key, value in published.kwargs.items():
        result.extend(["--" + key.replace("_", "-"), str(value)])
    return result


def test_public_cli_sealed_publication_to_plan_create_readback(published):
    runner = CliRunner()
    args = _cli_args(published)
    planned = runner.invoke(cli, args)
    assert planned.exit_code == 0, planned.output
    report = json.loads(planned.output)
    plan = json.loads(Path(report["plan"]).read_text())
    assert report["plan_hash"] == plan["plan_hash"]
    assert published.backend.calls == []
    assert plan["cost_scope"]["created_graphs"] == 0
    assert plan["cost_scope"]["model_calls"] == 0
    snapshot = decode_stage_snapshot(plan["create_request"]["definition"], "draft")
    sources = {source["type"]: source for source in snapshot.sources}
    assert set(sources) == {"ontology", "lakehouse_tables"}
    assert sources["ontology"]["artifactId"] == plan["source_ids"]["ontology"]
    assert sources["lakehouse_tables"]["artifactId"] == plan["sql_binding"]["lakehouse_id"]
    schemas = sources["lakehouse_tables"]["elements"][0]
    assert schemas["display_name"] == "Schemas" and schemas["type"] == "schema_grouping"
    dbo = schemas["children"][0]
    assert dbo["display_name"] == "dbo"
    tables = dbo["children"][0]
    assert tables["display_name"] == "Tables" and tables["type"] == "table_grouping"
    assert {table["display_name"] for table in tables["children"]} == set(plan["source_tables"])
    assert all(table["is_selected"] for table in tables["children"])
    assert "semantic-type:manufacturing.record" in sources["lakehouse_tables"]["dataSourceInstructions"]
    assert all(element["id"] == element["display_name"] for element in sources["ontology"]["elements"])
    assert plan["question_context"]["execution_verified"] is False
    assert "Never silently substitute GQL" in snapshot.instruction
    result = runner.invoke(cli, args + [
        "--live", "--approve-live", plan["plan_hash"], "--acknowledge-preview",
    ])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    journal = json.loads(Path(report["journal"]).read_text())
    assert journal["status"] == "draft-definition-verified-user-test-pending"
    assert journal["readiness"]["source_readback"] == "verified"
    assert journal["readiness"]["sql_execution"].startswith("unverified")
    assert journal["readiness"]["asking_user_permissions"] == "unverified"
    assert journal["readiness"]["published_stage"] == "not-created-draft-only"
    assert journal["delta_readbacks"]
    assert published.backend.agent_creates == 1
    assert all(method not in {"DELETE", "PATCH", "PUT"} for method, _, _ in published.backend.calls)
    assert all("updateDefinition" not in url for _, url, _ in published.backend.calls)


def test_plan_repeat_and_live_resume_do_not_recreate(published):
    plan = _plan(published)
    assert _plan(published) == plan
    first = _live(published, plan)
    second = _live(published, plan)
    assert first["agent_id"] == second["agent_id"]
    assert published.backend.agent_creates == 1


@pytest.mark.parametrize("change", [
    "workspace", "prefix", "journal-plan", "journal-workspace", "source-file",
    "materialized", "no-sql", "unready-sql", "native-definition", "delta-owner", "l3-evidence",
])
def test_foreign_changed_or_missing_source_rejected_before_cloud(published, change):
    kwargs = dict(published.kwargs)
    journal_path = kwargs["prototype_journal"]
    journal = json.loads(journal_path.read_text())
    if change == "workspace":
        kwargs["workspace_id"] = str(uuid.uuid4())
    elif change == "prefix":
        kwargs["name_prefix"] = "foreign"
    elif change == "journal-plan":
        journal["plan_hash"] = "foreign"
    elif change == "journal-workspace":
        journal["workspace_id"] = str(uuid.uuid4())
    elif change == "source-file":
        (kwargs["l4_run"] / "semantic-serving-projection.json").write_text("{}")
    elif change == "materialized":
        next((kwargs["materialize"] / "tables").glob("*.parquet")).unlink()
    elif change == "no-sql":
        journal["actions"]["create:lakehouse"]["metadata"]["properties"].pop("sqlEndpointProperties")
    elif change == "unready-sql":
        journal["actions"]["create:lakehouse"]["metadata"]["properties"]["sqlEndpointProperties"]["provisioningStatus"] = "InProgress"
    elif change == "native-definition":
        (kwargs["materialize"] / "native-bound" / "ontology.json").write_text('{"parts":[]}')
    elif change == "delta-owner":
        key = next(key for key in journal["actions"] if key.startswith("delta:"))
        journal["actions"][key]["path"] += "_foreign"
    elif change == "l3-evidence":
        next(kwargs["l3_root"].rglob("evidence-spans/*.json")).write_text("[]")
    journal_path.write_text(json.dumps(journal))
    with pytest.raises((ValueError, OSError, KeyError)):
        agent.publish_schema2_prototype_agent(**kwargs)
    assert published.backend.calls == []
    assert published.backend.agent_creates == 0


@pytest.mark.parametrize("mode", ["timeout", "missing-id", "rejected"])
def test_uncertain_or_rejected_create_retained_never_reposted(published, mode):
    plan = _plan(published)
    published.backend.create_mode = mode
    for _ in range(2):
        with pytest.raises((agent.Error, TimeoutError)):
            _live(published, plan)
    journal = json.loads((published.kwargs["out_state"] / "journal.json").read_text())
    assert journal["status"] == "partial-retained"
    assert journal["actions"]["create:data_agent"]["request_hash"]
    assert published.backend.agent_creates == 1
    assert not any(method == "DELETE" for method, _, _ in published.backend.calls)


def test_failed_lro_retained_without_retry_adoption_or_cleanup(published):
    plan = _plan(published)
    published.backend.create_mode = "lro"
    published.backend.operation_status = "Failed"
    with pytest.raises(agent.Error, match="Failed"):
        _live(published, plan)
    assert published.backend.agent_creates == 1
    assert published.backend.agent_id in published.backend.items
    with pytest.raises(agent.Error, match="Terminal"):
        _live(published, plan)
    assert published.backend.agent_creates == 1


def test_durable_lro_reference_resumes_after_read_timeout(published):
    plan = _plan(published)
    published.backend.create_mode = "lro"
    published.backend.operation_timeout_once = True
    with pytest.raises(TimeoutError):
        _live(published, plan)
    journal = json.loads((published.kwargs["out_state"] / "journal.json").read_text())
    assert journal["actions"]["create:data_agent"]["operation_id"] == published.backend.operation_id
    result = _live(published, plan)
    assert result["agent_id"] == published.backend.agent_id
    assert published.backend.agent_creates == 1


@pytest.mark.parametrize("change", ["source-denied", "sql-drift", "delta-version", "delta-history", "graph-definition"])
def test_actual_source_readback_gates_item_creation(published, change):
    plan = _plan(published)
    backend = published.backend
    if change == "source-denied":
        backend.source_denied = True
    elif change == "sql-drift":
        backend.items[plan["source_ids"]["lakehouse"]]["properties"]["sqlEndpointProperties"]["id"] = str(uuid.uuid4())
    elif change == "delta-version":
        next(iter(backend.tables.values()))["version"] = 1
    elif change == "delta-history":
        next(iter(backend.tables.values()))["history"]["prototype_plan_hash"] = "changed"
    elif change == "graph-definition":
        backend.definitions[plan["companion_id"]]["parts"] = []
    with pytest.raises(agent.Error):
        _live(published, plan)
    assert backend.agent_creates == 0
    journal = json.loads((published.kwargs["out_state"] / "journal.json").read_text())
    assert journal["status"] == "partial-retained"
    assert journal["readiness"]["source_readback"] == "in-progress"


def test_agent_definition_drift_retains_item_and_resume_reads_same_id(published):
    plan = _plan(published)
    published.backend.definition_drift = True
    with pytest.raises(agent.Error, match="definition/source"):
        _live(published, plan)
    journal = json.loads((published.kwargs["out_state"] / "journal.json").read_text())
    assert journal["status"] == "partial-retained"
    assert journal["actions"]["create:data_agent"]["item_id"] == published.backend.agent_id
    published.backend.definition_drift = False
    assert _live(published, plan)["agent_id"] == published.backend.agent_id
    assert published.backend.agent_creates == 1


def test_existing_name_no_adoption_and_wrong_approval_no_calls(published):
    plan = _plan(published)
    with pytest.raises(agent.Error, match="exact"):
        agent.publish_schema2_prototype_agent(
            **published.kwargs, live=True, approve_live="wrong", acknowledge_preview=True,
        )
    assert published.backend.calls == []
    foreign = str(uuid.uuid4())
    published.backend.items[foreign] = {
        "id": foreign, "type": "DataAgent", "displayName": plan["create_request"]["displayName"].upper(),
    }
    with pytest.raises(agent.Error, match="collision"):
        _live(published, plan)
    assert published.backend.agent_creates == 0


def test_live_requires_prior_plan_and_explicit_preview(published):
    with pytest.raises(agent.Error, match="offline"):
        agent.publish_schema2_prototype_agent(
            **published.kwargs, live=True, approve_live="unplanned", acknowledge_preview=True,
        )
    plan = _plan(published)
    with pytest.raises(agent.Error, match="acknowledge-preview"):
        agent.publish_schema2_prototype_agent(**published.kwargs, live=True, approve_live=plan["plan_hash"])
    assert published.backend.calls == []


@pytest.mark.parametrize("accepted_partial", [False, True])
def test_source_context_keeps_background_pending_and_sql_intentions(published, accepted_partial):
    handoff = agent._handoff(**{
        key: value for key, value in published.kwargs.items() if key != "out_state"
    })
    context = {
        **handoff.context,
        "business_context": {"organization_context": "Exact business background"},
        "problem_context": {"description": "Exact original problem"},
        "question_routing_context": {"questions": [{
            "question_id": "q6", "question": "Count distinct technicians.",
            "routing": {"answer_surface": "sql", "physical_binding_state": "unresolved"},
            "pending_requirements": ["Count grain requires reviewed canonical identities"],
        }]},
    }
    if accepted_partial:
        context["discovery_acceptance"] = {
            "acceptance_hash": "reviewed-acceptance-hash",
            "status": "partial_accepted", "accounted_chunks": 100, "total_chunks": 100,
        }
    definition = agent._definition(dataclasses.replace(handoff, context=context), "fixture", None)
    snapshot = decode_stage_snapshot(definition, "draft")
    assert canonical_json(context) in snapshot.instruction
    lakehouse = next(source for source in snapshot.sources if source["type"] == "lakehouse_tables")
    assert json.loads(lakehouse["metadata"]["schema2_question_context"]) == context
    assert "Count grain requires reviewed canonical identities" in snapshot.instruction
    assert '"physical_binding_state":"unresolved"' in snapshot.instruction
    assert handoff.evidence_binding["evidence_count"] > 0
    assert handoff.evidence_binding["agent_text_access"].startswith("not-established")
    assert ("do not infer absence from missing records" in snapshot.instruction) == accepted_partial
    assert ("does not establish semantic recall" in snapshot.instruction) == accepted_partial
    assert "asking user's permissions" in snapshot.instruction


def _configured_search(published):
    backend = published.backend
    source = DataSourceSpec(
        source_type="search", name="actual_configured_search",
        artifact_id=str(uuid.uuid4()), workspace_id=WORKSPACE, preview=True,
        metadata={"serviceEndpoint": "https://fixture.search.windows.net", "indexName": "evidence"},
        instructions="Old source-specific instructions must not become new routing authority.",
    )
    definition = {"parts": build_definition_parts(DataAgentSpec("existing-search-agent", sources=[source]))}
    item_id = str(uuid.uuid4())
    backend.items[item_id] = {"id": item_id, "displayName": "existing-search-agent", "type": "DataAgent"}
    backend.definitions[item_id] = definition
    path = published.kwargs["out_state"].parent / "native-search.json"
    path.write_text(json.dumps({
        "version": "native-search-source/1.0.0", "workspace_id": WORKSPACE,
        "agent_id": item_id, "definition": definition, "part_path": source.datasource_path(),
        "endpoint_pointer": "/metadata/serviceEndpoint", "index_pointer": "/metadata/indexName",
    }))
    published.kwargs["search_source"] = path
    return item_id


def _search_client(monkeypatch, result):
    import azure.search.documents
    calls = []

    class Search:
        def __init__(self, endpoint, index, credential):
            calls.append((endpoint, index))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def search(self, **kwargs):
            calls.append(kwargs)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(azure.search.documents, "SearchClient", Search)
    return calls


def test_only_captured_native_search_binding_is_preserved_and_read_checked(published, monkeypatch):
    existing = _configured_search(published)
    calls = _search_client(monkeypatch, [{"id": "evidence-1", "sourceUrl": "https://source.example/page"}])
    plan = _plan(published)
    assert published.backend.calls == [] and calls == []
    snapshot = decode_stage_snapshot(plan["create_request"]["definition"], "draft")
    source = next(source for source in snapshot.sources if source["type"] == "search")
    assert source["metadata"] == {"serviceEndpoint": "https://fixture.search.windows.net", "indexName": "evidence"}
    assert "Old source-specific instructions" not in source["dataSourceInstructions"]
    assert source["artifactId"] == plan["search_capability"]["source"]["artifactId"]
    result = _live(published, plan)
    assert result["search_readback"]["publisher_document_read"] is True
    assert result["search_readback"]["sealed_corpus_alignment"] == "unverified"
    assert result["readiness"]["asking_user_permissions"] == "unverified"
    assert calls == [("https://fixture.search.windows.net", "evidence"), {"search_text": "*", "top": 1}]
    assert existing != result["agent_id"]
    assert published.backend.agent_creates == 1


@pytest.mark.parametrize("problem", ["missing-native", "wrong-workspace", "non-search", "secret", "external-endpoint"])
def test_invalid_search_capability_rejected_offline(published, problem):
    _configured_search(published)
    path = published.kwargs["search_source"]
    capability = json.loads(path.read_text())
    if problem == "missing-native":
        capability["definition"] = {"parts": []}
    elif problem == "wrong-workspace":
        capability["workspace_id"] = str(uuid.uuid4())
    else:
        import base64
        part = next(part for part in capability["definition"]["parts"] if part["path"] == capability["part_path"])
        source = json.loads(base64.b64decode(part["payload"]))
        if problem == "non-search":
            source["type"] = "lakehouse"
        elif problem == "secret":
            source["metadata"]["apiKey"] = "DO-NOT-PERSIST-FIXTURE"
        else:
            source["metadata"]["serviceEndpoint"] = "https://untrusted.example"
        part["payload"] = base64.b64encode(json.dumps(source).encode()).decode()
    path.write_text(json.dumps(capability))
    with pytest.raises(agent.Error):
        _plan(published)
    assert not published.backend.calls
    assert not (published.kwargs["out_state"] / "plan.json").exists()


@pytest.mark.parametrize("problem", ["definition-drift", "permission-denied", "empty"])
def test_configured_search_failure_blocks_creation_not_silent_omission(published, monkeypatch, problem):
    existing = _configured_search(published)
    plan = _plan(published)
    if problem == "definition-drift":
        published.backend.definitions[existing]["parts"] = []
    _search_client(monkeypatch, PermissionError("fixture denied") if problem == "permission-denied" else [])
    with pytest.raises((agent.Error, PermissionError)):
        _live(published, plan)
    assert published.backend.agent_creates == 0
    assert json.loads((published.kwargs["out_state"] / "journal.json").read_text())["status"] == "partial-retained"


@pytest.mark.parametrize("method,url", [
    ("DELETE", f"{publication.API}/workspaces/{WORKSPACE}/dataAgents/00000000-0000-0000-0000-000000000001"),
    ("POST", f"{publication.API}/workspaces/{WORKSPACE}/dataAgents/00000000-0000-0000-0000-000000000001/updateDefinition"),
    ("GET", "https://untrusted.example/operations/00000000-0000-0000-0000-000000000001"),
    ("GET", f"{publication.API}/workspaces/00000000-0000-0000-0000-000000000001/items"),
])
def test_transport_rejects_mutation_foreign_workspace_and_cross_origin(published, method, url):
    plan = _plan(published)
    run = agent._AgentRun(published.kwargs["out_state"] / "journal.json", plan)
    with pytest.raises(agent.Error):
        run.request(method, url)
    assert published.backend.calls == []
    if method != "GET":
        with pytest.raises(agent.Error, match="read-only"):
            agent._ReadTransport(run).send(HttpRequest(method=method, url=url))


def test_agent_journal_and_source_plan_changes_fail_closed(published):
    plan = _plan(published)
    _live(published, plan)
    path = published.kwargs["out_state"] / "journal.json"
    journal = json.loads(path.read_text())
    journal["workspace_id"] = str(uuid.uuid4())
    path.write_text(json.dumps(journal))
    published.backend.calls.clear()
    with pytest.raises(agent.Error, match="journal"):
        _live(published, plan)
    assert published.backend.calls == []
    source = published.kwargs["prototype_journal"]
    changed = json.loads(source.read_text())
    changed["new_readback"] = "source snapshot changed"
    source.write_text(json.dumps(changed))
    with pytest.raises(agent.Error, match="plan"):
        _plan(published)
    assert published.backend.calls == []


def test_request_budget_and_output_overlap_fail_before_remote_operations(published):
    plan = _plan(published)
    run = agent._AgentRun(published.kwargs["out_state"] / "journal.json", plan)
    run.request_count = agent.MAX_FABRIC_REQUESTS
    with pytest.raises(agent.Error, match="budget"):
        run.request("GET", f"{publication.API}/workspaces/{WORKSPACE}/items")
    kwargs = {**published.kwargs, "out_state": published.kwargs["l4_run"] / "agent"}
    with pytest.raises(agent.Error, match="separate"):
        agent.publish_schema2_prototype_agent(**kwargs)
    assert published.backend.calls == []


def test_failed_resume_does_not_reuse_old_source_permission_readiness(published):
    plan = _plan(published)
    assert _live(published, plan)["readiness"]["publisher_source_permissions"].endswith("verified")
    published.backend.source_denied = True
    with pytest.raises(agent.Error):
        _live(published, plan)
    journal = json.loads((published.kwargs["out_state"] / "journal.json").read_text())
    assert journal["readiness"]["publisher_source_permissions"] == "unverified-this-invocation"
    assert journal["readiness"]["sql_binding"] == "unverified-this-invocation"
    assert published.backend.agent_creates == 1


def test_global_dry_run_rejects_local_live_before_publisher_call(published, monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "publish_schema2_prototype_agent", lambda **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(cli, [
        "--dry-run", *_cli_args(published), "--live",
        "--approve-live", "fixture-hash", "--acknowledge-preview",
    ])
    assert result.exit_code != 0
    assert "Global --dry-run cannot be combined" in result.output
    assert calls == [] and published.backend.calls == []
    assert not published.kwargs["out_state"].exists()


@pytest.fixture
def search_preflight(tmp_path, monkeypatch):
    """Isolate the credential gate; full sealed-source gating is covered above."""
    kwargs = {
        "prototype_journal": tmp_path / "publication-journal.json",
        "prototype_plan": tmp_path / "publication-plan.json",
        "materialize": tmp_path / "materialized", "l4_run": tmp_path / "l4",
        "l3_root": tmp_path / "l3", "workspace_id": WORKSPACE, "name_prefix": PREFIX,
        "out_state": tmp_path / "agent",
    }
    fixture = SimpleNamespace(kwargs=kwargs, backend=_Backend())
    _configured_search(fixture)
    monkeypatch.setattr(agent, "_handoff", lambda **_kwargs: None)

    def no_write(*_args, **_kwargs):
        pytest.fail("Unsafe Search configuration reached artifact persistence")

    monkeypatch.setattr(publication, "_atomic_json", no_write)
    return fixture


def _mutate_search(fixture, mutate):
    import base64

    path = fixture.kwargs["search_source"]
    capability = json.loads(path.read_text())
    part = next(
        part for part in capability["definition"]["parts"] if part["path"] == capability["part_path"]
    )
    source = json.loads(base64.b64decode(part["payload"]))
    mutate(source)
    part["payload"] = base64.b64encode(json.dumps(source).encode()).decode()
    path.write_text(json.dumps(capability))


@pytest.mark.parametrize("key", [
    "api-key", "API KEY", "api.key", "accessToken", "clientSecret", "refresh-token",
    "sharedAccessSignature", "connectionString", "authorizationHeader", "authentication",
    "identity", "clientId", "private-key", "ＡＰＩ－ＫＥＹ",
])
@pytest.mark.parametrize("location", ["metadata", "nested-arrays", "element"])
def test_secret_key_variants_recursively_fail_before_any_plan_write(search_preflight, key, location):
    marker = "fixture-sensitive-value"

    def mutate(source):
        if location == "metadata":
            source["metadata"][key] = marker
        elif location == "nested-arrays":
            source["metadata"]["configuration"] = [[{"nested": [{key: marker}]}]]
        else:
            source["elements"] = [{
                "display_name": "Index", "children": [{"display_name": "Child", key: marker}],
            }]

    _mutate_search(search_preflight, mutate)
    with pytest.raises(agent.Error) as caught:
        _plan(search_preflight)
    assert marker not in str(caught.value)
    assert not search_preflight.kwargs["out_state"].exists()
    assert search_preflight.backend.calls == []


@pytest.mark.parametrize("case", [
    "userinfo-url", "token-query", "signed-query", "encoded-query", "fragment-token",
    "bearer-value", "quoted-secret", "unknown-auth", "unknown-metadata", "unknown-root",
])
def test_credential_strings_and_unsupported_connection_fields_fail_closed(search_preflight, case):
    marker = "fixture-sensitive-value"
    values = {
        "userinfo-url": f"https://user:{marker}@source.example/path",
        "token-query": f"https://source.example/path?access_token={marker}",
        "signed-query": f"https://source.example/path?sig={marker}",
        "encoded-query": f"https://source.example/path%3Fapi-key%3D{marker}",
        "fragment-token": f"https://source.example/path#access_token={marker}",
        "bearer-value": f"Bearer {marker}",
        "quoted-secret": json.dumps({"clientSecret": marker}),
    }

    def mutate(source):
        if case in values:
            source["userDescription"] = values[case]
        elif case == "unknown-auth":
            source["authenticationOptions"] = {"mode": "user-identity"}
        elif case == "unknown-root":
            source["connectionExtension"] = "unreviewed"
        else:
            source["metadata"]["opaqueSetting"] = "unreviewed"

    _mutate_search(search_preflight, mutate)
    with pytest.raises(agent.Error) as caught:
        _plan(search_preflight)
    assert marker not in str(caught.value)
    assert not search_preflight.kwargs["out_state"].exists()
    assert search_preflight.backend.calls == []


def test_reviewed_non_auth_search_options_are_preserved(search_preflight):
    _mutate_search(search_preflight, lambda source: source["metadata"].update({
        "searchType": "hybrid", "numberOfDocuments": "10",
    }))
    capability = agent._search_capability(search_preflight.kwargs["search_source"], WORKSPACE)
    assert capability["source"]["metadata"]["searchType"] == "hybrid"
    assert capability["source"]["metadata"]["numberOfDocuments"] == "10"
    assert not search_preflight.kwargs["out_state"].exists()


def test_instruction_boundary_exact_limit_and_oversize_offline_rejection(published, monkeypatch):
    handoff = agent._handoff(**{
        key: value for key, value in published.kwargs.items() if key != "out_state"
    })
    context = {"fixture_context": ""}
    minimal = dataclasses.replace(handoff, context=context)
    baseline = len(decode_stage_snapshot(agent._definition(minimal, "fixture", None), "draft").instruction)
    padding = agent.MAX_AGENT_INSTRUCTION_CHARACTERS - baseline
    assert padding > 0
    exact = dataclasses.replace(handoff, context={"fixture_context": "x" * padding})
    definition = agent._definition(exact, "fixture", None)
    instruction = decode_stage_snapshot(definition, "draft").instruction
    assert len(instruction) == 15_000
    assert canonical_json(exact.context) in instruction
    oversized = dataclasses.replace(handoff, context={"fixture_context": "x" * (padding + 1)})
    monkeypatch.setattr(agent, "_handoff", lambda **_kwargs: oversized)
    with pytest.raises(agent.Error) as caught:
        _plan(published)
    assert "15001 characters" in str(caught.value)
    assert "15000" in str(caught.value)
    assert "x" * 100 not in str(caught.value)
    assert not (published.kwargs["out_state"] / "plan.json").exists()
    assert not (published.kwargs["out_state"] / "journal.json").exists()
    assert published.backend.calls == []
