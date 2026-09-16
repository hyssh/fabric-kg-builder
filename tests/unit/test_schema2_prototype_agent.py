"""Offline, genuine sealed-source/publication-journal to native Data Agent path."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.deploy import schema2_prototype as publication
from fabric_kg_builder.deploy import schema2_prototype_agent as agent
from fabric_kg_builder.deploy import schema2_prototype_reconcile as reconcile
from fabric_kg_builder.knowledge.data_agent import (
    DataAgentSpec, DataSourceSpec, build_definition_parts, decode_stage_snapshot,
)
from fabric_kg_builder.knowledge.transport import HttpRequest
from tests.unit.test_l5a_structured_publication import _inputs

WORKSPACE = "11111111-1111-4111-8111-111111111111"
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
        self.graph_lro = False
        self.graph_id = None
        self.graph_operation_id = str(uuid.uuid4())
        self.graph_operation_status = "Succeeded"

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs.get("json"))))
        path = url.split("?", 1)[0]
        if path.endswith("/items") and method == "GET":
            return _Response({"value": list(self.items.values())})
        if "/operations/" in path:
            if self.graph_operation_id in path:
                return _Response(
                    {"id": self.graph_id} if path.endswith("/result")
                    else {"status": self.graph_operation_status}
                )
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
                "kind": "TABLE",
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
                "workspaceId": WORKSPACE, "description": body["description"],
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
            if collection == "graphModels":
                self.graph_id = item_id
                if self.graph_lro:
                    return _Response({}, 202, {
                        "x-ms-operation-id": self.graph_operation_id,
                        "Location": f"{publication.API}/operations/{self.graph_operation_id}",
                    })
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
def published(tmp_path, monkeypatch, publication_graph_lro):
    source = _inputs(tmp_path / "sealed")["source"]
    # The existing helper returns its final sealed L3 manifest in memory.
    # Persist that exact manifest for the public path-based source reader.
    l3_root = tmp_path / "sealed" / ".fkg" / "l3"
    next(l3_root.rglob("output-manifest.json")).write_text(
        canonical_json(source.input_manifest.model_dump(mode="json")) + "\n",
    )
    backend = _Backend()
    backend.graph_lro = publication_graph_lro
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
                "readback": proof,
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
        "approved_limitations": (publication.METADATA_ONLY_ALIASES_LIMITATION,),
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
    assert "publication_runtime_repair" not in plan
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


@pytest.fixture
def reviewed_context(published, monkeypatch):
    handoff = agent._handoff(**{
        key: value for key, value in published.kwargs.items() if key != "out_state"
    })
    context = copy.deepcopy(handoff.context)
    context["business_context"]["organization_context"] = "Original business and obsolete design instructions"
    context["window_run_scope"] = {"scope_notice": "Only the approved two-document prefix is represented."}
    context["question_routing_context"] = {"questions": [{
        "question_id": "q:sql-count", "question": "How many approved records?",
        "preferred_execution_surface": "sql",
        "routing": {"backend": "sql", "filters": [], "grain": None},
        "pending_requirements": ["Preserve canonical count grain; unresolved physical binding."],
    }]}
    context["export_hash"] = canonical_sha256({
        key: value for key, value in context.items() if key != "export_hash"
    })
    handoff = dataclasses.replace(handoff, context=context)
    monkeypatch.setattr(agent, "_handoff", lambda **_kwargs: handoff)
    path = published.kwargs["out_state"].parent / "runtime-context-review.json"
    value = {
        "version": agent.RUNTIME_CONTEXT_REVIEW_VERSION,
        "domain_contract_hash": context["domain_contract_hash"],
        "actor": "reviewer@example.test",
        "rationale": "Keep business meaning; exclude obsolete compiler-editing directions.",
        "organization_context": "Support grounded business answers for the approved two-document prefix.",
    }
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")
    published.kwargs["runtime_context_review"] = path
    return SimpleNamespace(published=published, handoff=handoff, path=path, value=value)


def test_runtime_review_changes_only_global_organization_context(reviewed_context):
    fixture = reviewed_context
    original = copy.deepcopy(fixture.handoff.context)
    original_definition = agent._definition(fixture.handoff, "fixture", None)
    review = agent._runtime_context_review(fixture.path, original)
    definition = agent._definition(fixture.handoff, "fixture", None, review)
    before = decode_stage_snapshot(original_definition, "draft")
    after = decode_stage_snapshot(definition, "draft")
    runtime = json.loads(after.instruction.split("(export_hash identifies", 1)[1].split(":\n", 1)[1])
    expected = copy.deepcopy(original)
    expected["business_context"]["organization_context"] = fixture.value["organization_context"]
    assert runtime == expected
    assert fixture.handoff.context == original
    assert before.sources == after.sources
    source = next(item for item in after.sources if item["type"] == "lakehouse_tables")
    assert json.loads(source["metadata"]["schema2_question_context"]) == original
    assert before.instruction.split("Exact sealed domain")[0] == after.instruction.split("Explicitly reviewed")[0]
    assert "Never silently substitute GQL" in after.instruction
    assert "cite only retrieved quotes" in after.instruction


@pytest.mark.parametrize("change", [
    "wrong-contract", "wrong-version", "blank-actor", "blank-rationale", "blank-text",
    "unknown-field", "missing-field", "nonstring", "malformed", "duplicate",
    "nonfinite", "array", "oversized-file", "oversized-text", "unapproved",
    "unknown-encoding", "null-encoding", "object-encoding",
])
def test_runtime_review_rejects_invalid_inputs_before_plan(reviewed_context, change):
    fixture = reviewed_context
    value = copy.deepcopy(fixture.value)
    if change == "wrong-contract":
        value["domain_contract_hash"] = "0" * 64
    elif change == "wrong-version":
        value["version"] = "2"
    elif change.startswith("blank-"):
        key = {"actor": "actor", "rationale": "rationale", "text": "organization_context"}[change[6:]]
        value[key] = " \n\t"
    elif change == "unknown-field":
        value["question_routing_context"] = {}
    elif change == "missing-field":
        del value["rationale"]
    elif change == "nonstring":
        value["organization_context"] = ["not text"]
    elif change == "oversized-text":
        value["organization_context"] = "x" * 15_001
    elif change == "unapproved":
        fixture.handoff.context["approval_status"] = "draft"
    elif change.endswith("-encoding"):
        value["routing_encoding"] = {
            "unknown-encoding": "columns-v2", "null-encoding": None, "object-encoding": {},
        }[change]
    raw = canonical_json(value)
    raw = {
        "malformed": "{", "duplicate": raw[:-1] + ',"actor":"another"}',
        "nonfinite": raw.replace('"actor":', '"actor":NaN,"unused":', 1),
        "array": "[]", "oversized-file": " " * (agent.MAX_RUNTIME_CONTEXT_REVIEW_BYTES + 1),
    }.get(change, raw)
    fixture.path.write_text(raw, encoding="utf-8")
    with pytest.raises(agent.Error):
        _plan(fixture.published)
    assert not fixture.published.kwargs["out_state"].exists()
    assert fixture.published.backend.calls == []


@pytest.mark.parametrize("columns", [False, True])
def test_runtime_review_cli_discloses_binding_and_preserves_source(reviewed_context, columns):
    fixture = reviewed_context
    if columns:
        fixture.value["routing_encoding"] = "columns-v1"
        fixture.path.write_text(canonical_json(fixture.value))
    result = CliRunner().invoke(cli, _cli_args(fixture.published))
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    plan = json.loads(Path(report["plan"]).read_text())
    review = report["runtime_context_review"]
    assert review == plan["runtime_context_review"]
    assert review["content"] == fixture.value
    assert review["path"] == str(fixture.path.resolve())
    assert review["review_hash"] == canonical_sha256(fixture.value)
    assert review["file_sha256"] == agent.hashlib.sha256(fixture.path.read_bytes()).hexdigest()
    assert plan["question_context"] == fixture.handoff.context
    assert plan["plan_hash"] == canonical_sha256({
        key: value for key, value in plan.items() if key != "plan_hash"
    })
    assert fixture.published.backend.calls == []


@pytest.mark.parametrize("change", [
    "text", "actor", "rationale", "path", "format", "removed", "plan", "encoding",
])
def test_review_drift_blocks_live_before_any_requests(reviewed_context, change):
    fixture = reviewed_context
    plan = _plan(fixture.published)
    if change == "removed":
        fixture.published.kwargs.pop("runtime_context_review")
    elif change == "path":
        other = fixture.path.with_name("another-review.json")
        other.write_bytes(fixture.path.read_bytes())
        fixture.published.kwargs["runtime_context_review"] = other
    elif change == "format":
        fixture.path.write_text(json.dumps(fixture.value, indent=2), encoding="utf-8")
    elif change == "plan":
        plan_path = fixture.published.kwargs["out_state"] / "plan.json"
        altered = json.loads(plan_path.read_text())
        altered["runtime_context_review"]["content"]["actor"] = "changed"
        altered["plan_hash"] = canonical_sha256({
            key: value for key, value in altered.items() if key != "plan_hash"
        })
        plan_path.write_text(canonical_json(altered))
    elif change == "encoding":
        fixture.path.write_text(canonical_json({**fixture.value, "routing_encoding": "columns-v1"}))
    else:
        value = {**fixture.value, "organization_context" if change == "text" else change: "changed"}
        fixture.path.write_text(canonical_json(value), encoding="utf-8")
    with pytest.raises(agent.Error, match="plan or its publication/source bindings changed"):
        _live(fixture.published, plan)
    assert fixture.published.backend.calls == []


@pytest.mark.parametrize("columns", [False, True])
def test_review_definition_drift_and_exact_resume(reviewed_context, columns):
    fixture = reviewed_context
    if columns:
        fixture.value["routing_encoding"] = "columns-v1"
        fixture.path.write_text(canonical_json(fixture.value))
    plan = _plan(fixture.published)
    _live(fixture.published, plan)
    assert fixture.published.backend.agent_creates == 1
    backend = fixture.published.backend
    expected = copy.deepcopy(backend.definitions[backend.agent_id])
    unencoded_review = {"content": {
        key: value for key, value in fixture.value.items() if key != "routing_encoding"
    }} if columns else None
    backend.definitions[backend.agent_id] = agent._definition(
        fixture.handoff, plan["create_request"]["displayName"], None, unencoded_review,
    )
    backend.calls.clear()
    with pytest.raises(agent.Error, match="definition/source selection readback mismatch"):
        _live(fixture.published, plan)
    assert backend.agent_creates == 1
    backend.definitions[backend.agent_id] = expected
    assert _live(fixture.published, plan)["runtime_context_review"] == plan["runtime_context_review"]
    assert backend.agent_creates == 1
    fixture.path.write_text(canonical_json({**fixture.value, "actor": "another"}))
    backend.calls.clear()
    with pytest.raises(agent.Error, match="bindings changed"):
        _live(fixture.published, plan)
    assert backend.calls == []


@pytest.mark.parametrize("columns", [False, True])
def test_runtime_review_never_reduces_remaining_context_to_fit(reviewed_context, columns):
    fixture = reviewed_context
    if columns:
        fixture.path.write_text(canonical_json({**fixture.value, "routing_encoding": "columns-v1"}))
    fixture.handoff.context["question_routing_context"]["questions"][0]["pending_requirements"] = ["x" * 15_000]
    original = copy.deepcopy(fixture.handoff.context)
    with pytest.raises(agent.Error, match="limit is 15000.*no content was truncated"):
        _plan(fixture.published)
    assert fixture.handoff.context == original
    assert not (fixture.published.kwargs["out_state"] / "plan.json").exists()
    assert fixture.published.backend.calls == []


def test_review_can_fit_oversized_original_without_losing_source_text(reviewed_context):
    fixture = reviewed_context
    fixture.handoff.context["business_context"]["organization_context"] = "Original context. " * 1000
    original = copy.deepcopy(fixture.handoff.context)
    with pytest.raises(agent.Error, match="limit is 15000"):
        agent._definition(fixture.handoff, "fixture", None)
    plan = _plan(fixture.published)
    snapshot = decode_stage_snapshot(plan["create_request"]["definition"], "draft")
    assert len(snapshot.instruction) <= 15_000
    assert fixture.value["organization_context"] in snapshot.instruction
    source = next(item for item in snapshot.sources if item["type"] == "lakehouse_tables")
    assert json.loads(source["metadata"]["schema2_question_context"]) == original
    assert plan["question_context"] == original == fixture.handoff.context
    assert fixture.published.backend.calls == []


def test_default_plan_has_no_review_and_replays_unchanged(published):
    plan = _plan(published)
    assert "runtime_context_review" not in plan
    published.kwargs["runtime_context_review"] = None
    assert _plan(published) == plan
    snapshot = decode_stage_snapshot(plan["create_request"]["definition"], "draft")
    assert "Explicitly reviewed" not in snapshot.instruction
    assert canonical_json(plan["question_context"]) in snapshot.instruction


def _expand_routing_columns(encoded):
    routing = copy.deepcopy(encoded)
    assert routing.pop("encoding") == "columns-v1"
    columns = routing.pop("columns")
    routing_columns = routing.pop("routing_columns")
    routing["questions"] = [dict(zip(columns, row, strict=True)) for row in routing["questions"]]
    for question in routing["questions"]:
        question["routing"] = dict(zip(routing_columns, question["routing"], strict=True))
    return routing


@pytest.mark.parametrize("questions", [[], [
    {
        "question_id": "cq:2", "question": "Second?", "business_critical": False,
        "routing": {"backend": "sql", "filters": [None, {"ids": ["z", "a"]}], "grain": None},
        "pending_requirements": [None, "", {"requirement_id": "r:2", "required": True}],
    },
    {
        "pending_requirements": [], "business_critical": True, "question": "First?",
        "question_id": "cq:1", "routing": {"grain": "", "filters": [], "backend": "sql"},
    },
]])
def test_routing_columns_lossless_roundtrip(questions):
    original = {"context_version": "1.0", "context_hash": "a" * 64, "questions": questions}
    before = copy.deepcopy(original)
    encoded = json.loads(canonical_json(agent._routing_columns(original)))
    assert _expand_routing_columns(encoded) == before
    assert original == before
    assert encoded["context_hash"] == original["context_hash"]


@pytest.mark.parametrize("context", [
    None, {}, {"questions": None}, {"questions": [None]},
    {"questions": [{"routing": None}]},
    {"questions": [], "columns": []},
    {"questions": [{"routing": {}}, {"routing": {}, "extra": None}]},
    {"questions": [{"routing": {}}, {"routing": {"extra": None}}]},
])
def test_routing_columns_rejects_nonhomogeneous_or_ambiguous_input(context):
    with pytest.raises(agent.Error, match="columns-v1"):
        agent._routing_columns(context)


def test_columns_review_preserves_expanded_global_and_original_source(reviewed_context):
    fixture = reviewed_context
    original = copy.deepcopy(fixture.handoff.context)
    fixture.value["routing_encoding"] = "columns-v1"
    fixture.path.write_text(canonical_json(fixture.value))
    plan = _plan(fixture.published)
    snapshot = decode_stage_snapshot(plan["create_request"]["definition"], "draft")
    runtime = json.loads(snapshot.instruction.split("(export_hash identifies", 1)[1].split(":\n", 1)[1])
    assert runtime["question_routing_context"]["encoding"] == "columns-v1"
    runtime["question_routing_context"] = _expand_routing_columns(runtime["question_routing_context"])
    expected = copy.deepcopy(original)
    expected["business_context"]["organization_context"] = fixture.value["organization_context"]
    assert runtime == expected
    assert "context_hash identifies expanded content" in snapshot.instruction
    source = next(item for item in snapshot.sources if item["type"] == "lakehouse_tables")
    assert json.loads(source["metadata"]["schema2_question_context"]) == original
    assert plan["question_context"] == original == fixture.handoff.context
    assert plan["runtime_context_review"]["content"]["routing_encoding"] == "columns-v1"
    assert _plan(fixture.published) == plan
    fixture.path.write_text(canonical_json({
        key: value for key, value in fixture.value.items() if key != "routing_encoding"
    }))
    with pytest.raises(agent.Error, match="bindings changed"):
        _live(fixture.published, plan)
    assert fixture.published.backend.calls == []


@pytest.mark.skipif(
    not os.environ.get("FKG_TEST_APPROVED_DOMAIN_CONTRACT"),
    reason="Optional actual-contract sizing: set FKG_TEST_APPROVED_DOMAIN_CONTRACT",
)
def test_actual_approved_contract_columns_instruction_size(published, monkeypatch):
    """Sizing only: no sealed source or deployment is asserted by placeholder hashes."""
    from fabric_kg_builder.domain.service import load_domain_contract
    from fabric_kg_builder.serving import structured_publication as serving

    contract = load_domain_contract(Path(os.environ["FKG_TEST_APPROVED_DOMAIN_CONTRACT"]))
    assert contract.approval.status == "approved"
    contract_hash = serving.compute_contract_hash(contract)
    source = SimpleNamespace(
        root=published.source.root,
        projection=SimpleNamespace(sealed_domain_contract_hash=contract_hash, projection_hash="0" * 64),
        manifest=published.source.manifest,
    )
    handoff = agent._handoff(**{
        key: value for key, value in published.kwargs.items() if key != "out_state"
    })
    with monkeypatch.context() as sizing:
        sizing.setattr(serving, "_load_source_tables", lambda _source: None)
        sizing.setattr(
            serving, "_publication_authority",
            lambda _tables: (contract, {"domain_contract_hash": contract_hash}),
        )
        context = serving.export_serving_question_context(source)
    handoff = dataclasses.replace(handoff, context=context)
    text = (
        "Microsoft Surface servicing from supplied official guides. Isolated prototype for "
        "qualified technicians; not production maintenance authorization."
    )
    review = {"content": {"organization_context": text}}
    with pytest.raises(agent.Error, match="limit is 15000"):
        agent._definition(handoff, "offline-sizing-only", None, review)
    review["content"]["routing_encoding"] = "columns-v1"
    snapshot = decode_stage_snapshot(agent._definition(handoff, "offline-sizing-only", None, review), "draft")
    runtime = json.loads(snapshot.instruction.split("(export_hash identifies", 1)[1].split(":\n", 1)[1])
    encoded = runtime["question_routing_context"]
    runtime["question_routing_context"] = _expand_routing_columns(encoded)
    expected = copy.deepcopy(context)
    expected["business_context"]["organization_context"] = text
    assert runtime == expected
    source_metadata = next(item for item in snapshot.sources if item["type"] == "lakehouse_tables")
    assert source_metadata["metadata"]["schema2_question_context"] == canonical_json(context)
    assert len(snapshot.instruction) <= 15_000
    assert published.backend.calls == []
    print(
        f"Sizing only: contract={contract_hash}; organization={len(text)}; "
        f"routing={len(canonical_json(context['question_routing_context']))}"
        f"->{len(canonical_json(encoded))}; global={len(snapshot.instruction)}"
    )


def test_new_agent_revision_does_not_alias_legacy_compiler_identity(monkeypatch):
    identity = {
        "prototype_publication": publication._compiler_hash(),
        "agent_publisher": agent._LEGACY_AGENT_PUBLISHER_HASH,
        "agent_definition": agent.hashlib.sha256(Path(agent.data_agent.__file__).read_bytes()).hexdigest(),
    }
    legacy = canonical_sha256(identity)
    identity["agent_publisher"] = agent.hashlib.sha256(Path(agent.__file__).read_bytes()).hexdigest()
    expected = canonical_sha256(identity)
    assert expected != legacy
    assert agent._compiler_hash() == expected
    assert agent._compiler_hash(runtime_review=True) == expected
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: (
        read_bytes(path) + b"\n" if path == Path(agent.__file__) else read_bytes(path)
    ))
    assert agent._compiler_hash() != expected


@pytest.fixture
def repaired_publication(published, monkeypatch):
    monkeypatch.setattr(publication, "_compiler_hash", lambda: "reviewed-runtime")
    kwargs = published.kwargs
    recovery = {
        "plan_path": kwargs["prototype_plan"], "journal_path": kwargs["prototype_journal"],
        "materialize": kwargs["materialize"], "l4_run": kwargs["l4_run"], "l3_root": kwargs["l3_root"],
        "kind": "graph", "item_id": published.backend.graph_id,
        "review_path": kwargs["out_state"].parent / "publication-runtime-review.json",
        "returned_id_runtime_repair": True,
    }
    preview = reconcile.reconcile_prototype_create(**recovery)
    reconcile.reconcile_prototype_create(
        **recovery, accept_review=preview["review_hash"],
        actor="fixture reviewer", rationale="Reviewed exact returned-ID readback runtime repair",
    )
    published.backend.calls.clear()
    return published


@pytest.mark.parametrize("publication_graph_lro", [False, True], indirect=True)
def test_reviewed_returned_repair_offline_binding_and_fresh_live_proof(repaired_publication):
    case = repaired_publication
    original_plan = case.kwargs["prototype_plan"].read_bytes()
    original_journal = case.kwargs["prototype_journal"].read_bytes()
    artifacts = reconcile._artifact_digests(case.kwargs["materialize"])
    receipt = json.loads(original_journal)["runtime_repair"]
    plan = _plan(case)
    binding = plan["publication_runtime_repair"]
    assert binding["policy"] == reconcile.RETURNED_ID_POLICY
    assert binding["receipt_hash"] == canonical_sha256(receipt)
    assert binding["review_hash"] == receipt["review_hash"]
    assert binding["original_plan_hash"] == case.publication_plan["plan_hash"]
    assert binding["original_plan_bytes_hash"] == agent.hashlib.sha256(original_plan).hexdigest()
    assert binding["original_compiler_hash"] == case.publication_plan["compiler_hash"]
    assert binding["current_compiler_hash"] == "reviewed-runtime"
    assert binding["candidate_plan_hash"] != case.publication_plan["plan_hash"]
    candidate = reconcile.recompile_plan(
        case.kwargs["prototype_plan"], case.kwargs["prototype_journal"],
        case.kwargs["materialize"], case.kwargs["l4_run"], case.kwargs["l3_root"],
    )
    assert binding["candidate_plan_hash"] == candidate["plan_hash"]
    assert binding["semantic_comparison_hash"] == canonical_sha256(reconcile._semantic(candidate))
    assert reconcile._semantic(candidate) == reconcile._semantic(case.publication_plan)
    assert binding["artifact_digests_hash"] == canonical_sha256(artifacts)
    assert binding["item_id"] == case.backend.graph_id
    assert plan["question_context"]["execution_verified"] is False
    assert case.backend.calls == []
    assert _plan(case) == plan

    result = _live(case, plan)
    assert result["status"] == "draft-definition-verified-user-test-pending"
    assert case.backend.agent_creates == 1
    create = next(i for i, (method, url, _) in enumerate(case.backend.calls)
                  if method == "POST" and url.endswith("/dataAgents"))
    reads = [(method, url) for method, url, _ in case.backend.calls[:create]]
    assert ("GET", f"{publication.API}/workspaces/{WORKSPACE}/graphModels/{case.backend.graph_id}") in reads
    assert ("POST", f"{publication.API}/workspaces/{WORKSPACE}/graphModels/{case.backend.graph_id}/getDefinition") in reads
    if case.backend.graph_lro:
        assert ("GET", f"{publication.API}/operations/{case.backend.graph_operation_id}") in reads
        assert ("GET", f"{publication.API}/operations/{case.backend.graph_operation_id}/result") in reads
    assert all(method == "GET" or method == "POST" and (
        url.split("?", 1)[0].endswith("/getDefinition")
        or url.split("?", 1)[0].endswith("/executeQuery")
    ) for method, url in reads)
    assert case.kwargs["prototype_plan"].read_bytes() == original_plan
    assert case.kwargs["prototype_journal"].read_bytes() == original_journal
    assert reconcile._artifact_digests(case.kwargs["materialize"]) == artifacts


@pytest.mark.parametrize("change", [
    "missing-receipt", "unaccepted", "tampered-review", "wrong-bound-compiler",
    "changed-current-code", "changed-agent-code", "wrong-plan-bytes", "wrong-source",
    "wrong-materialized", "unsupported-kind", "unsupported-policy", "operator-owned",
    "changed-create-evidence", "unready-companion", "changed-receipt-after-plan",
    "tampered-agent-plan",
])
def test_repaired_agent_rejects_drift_before_network(repaired_publication, monkeypatch, change):
    case = repaired_publication
    plan = _plan(case)
    journal_path = case.kwargs["prototype_journal"]
    journal = json.loads(journal_path.read_text())
    receipt = journal["runtime_repair"]
    if change == "missing-receipt":
        del journal["runtime_repair"]
    elif change == "unaccepted":
        receipt["status"] = "unaccepted"
    elif change == "tampered-review":
        receipt["review"]["item_id"] = str(uuid.uuid4())
    elif change == "wrong-bound-compiler":
        receipt["review"]["current_compiler_hash"] = "foreign"
        receipt["review_hash"] = canonical_sha256(receipt["review"])
    elif change == "changed-current-code":
        monkeypatch.setattr(publication, "_compiler_hash", lambda: "unreviewed-runtime")
    elif change == "changed-agent-code":
        monkeypatch.setattr(agent, "_compiler_hash", lambda **_kwargs: "unreviewed-agent")
    elif change == "wrong-plan-bytes":
        path = case.kwargs["prototype_plan"]
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "wrong-source":
        (case.kwargs["l4_run"] / "semantic-serving-projection.json").write_text("{}")
    elif change == "wrong-materialized":
        next((case.kwargs["materialize"] / "tables").glob("*.parquet")).unlink()
    elif change == "unsupported-kind":
        receipt["review"]["kind"] = "ontology"
        receipt["review_hash"] = canonical_sha256(receipt["review"])
    elif change == "unsupported-policy":
        receipt["policy"] = reconcile.POLICY
    elif change == "operator-owned":
        journal["actions"]["create:ontology"]["ownership"] = "operator-reconciled"
    elif change == "changed-create-evidence":
        journal["actions"]["create:graph"]["returned_item_id"] = str(uuid.uuid4())
    elif change == "unready-companion":
        journal["ontology_readiness"] = "GraphNotQueryable"
    elif change == "changed-receipt-after-plan":
        receipt["rationale"] += " Updated after agent approval."
    elif change == "tampered-agent-plan":
        path = case.kwargs["out_state"] / "plan.json"
        value = json.loads(path.read_text())
        value["publication_runtime_repair"]["current_compiler_hash"] = "foreign"
        value["plan_hash"] = canonical_sha256({k: v for k, v in value.items() if k != "plan_hash"})
        path.write_text(canonical_json(value))
    journal_path.write_text(canonical_json(journal))
    with pytest.raises((ValueError, OSError)):
        _live(case, plan)
    assert case.backend.calls == []
    assert case.backend.agent_creates == 0
    assert not (case.kwargs["out_state"] / "journal.json").exists()


@pytest.mark.parametrize("publication_graph_lro", [True], indirect=True)
@pytest.mark.parametrize("change", ["definition", "operation", "receipt-during-proof"])
def test_repaired_agent_failed_fresh_proof_prevents_create(repaired_publication, monkeypatch, change):
    case = repaired_publication
    plan = _plan(case)
    if change == "definition":
        case.backend.definitions[case.backend.graph_id]["parts"].pop()
    elif change == "operation":
        case.backend.graph_operation_status = "Failed"
    elif change == "receipt-during-proof":
        request = case.backend.request

        def changed(method, url, **kwargs):
            response = request(method, url, **kwargs)
            if url.endswith("/getDefinition"):
                path = case.kwargs["prototype_journal"]
                journal = json.loads(path.read_text())
                journal["runtime_repair"]["rationale"] += " Changed during proof."
                path.write_text(canonical_json(journal))
            return response

        monkeypatch.setattr(case.backend, "request", changed)
    with pytest.raises(agent.Error):
        _live(case, plan)
    assert case.backend.calls
    assert case.backend.agent_creates == 0
    assert all(method == "GET" or method == "POST" and url.endswith("/getDefinition")
               for method, url, _ in case.backend.calls)
    assert not (case.kwargs["out_state"] / "journal.json").exists()
