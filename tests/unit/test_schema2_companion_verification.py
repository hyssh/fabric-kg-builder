"""Native managed Graph dialect and immutable, separately approved read-only proof."""

import base64
import copy
import json
import uuid

import pyarrow as pa
import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.deploy import schema2_companion_verification as v
from fabric_kg_builder.deploy import schema2_prototype as p
from fabric_kg_builder.deploy import schema2_prototype_reconcile as r
from fabric_kg_builder.serving.graph_model import encode_parts_for_api
from tests.unit.test_schema2_graph_compatibility import compiled  # noqa: F401
from tests.unit.test_schema2_returned_id_runtime_repair import owned, _accept  # noqa: F401
from tests.unit.test_schema2_prototype_reconcile import WORKSPACE, _Response

LAKEHOUSE = "22222222-2222-4222-8222-222222222222"
COMPANION = "33333333-3333-4333-8333-333333333333"
COUNTS, CONTENT, READINESS = p._Run.graph_counts, p._Run.graph_content, p._Run.companion_readiness


def _encoded(payloads):
    return {"parts": encode_parts_for_api([
        {"path": name, "payload_json": value} for name, value in payloads.items()
    ])}


def _decoded(definition):
    return {part["path"]: json.loads(base64.b64decode(part["payload"])) for part in definition["parts"]}


def _native(compilation, ontology, workspace=WORKSPACE, lakehouse=LAKEHOUSE, name="fixture_companion"):
    payloads = p._definition_payloads(ontology)
    schema = p._GRAPH_SCHEMA_ROOT + "graphInstance/definition/"
    graph = {"$schema": schema + "graphType/1.0.0/schema.json", "nodeTypes": [], "edgeTypes": []}
    bindings = {"$schema": schema + "graphDefinition/1.0.0/schema.json", "nodeTables": [], "edgeTables": []}
    sources = {"$schema": p._INSTANCE_SOURCES_SCHEMA, "dataSources": []}
    for path, part in payloads.items():
        if "/DataBindings/" in path:
            entity = payloads[f"EntityTypes/{path.split('/')[1]}/definition.json"]
            properties = {item["id"]: item["name"] for item in entity["properties"]}
            configuration = part["dataBindingConfiguration"]
            table_id = configuration["sourceTableProperties"]["sourceTableName"]
            table = compilation.tables[table_id]
            mappings = [{
                "propertyName": properties[item["targetPropertyId"]], "sourceColumn": item["sourceColumnName"],
            } for item in configuration["propertyBindings"]]
            types = []
            for mapping in mappings:
                datatype = table.schema.field(mapping["sourceColumn"]).type
                value = (
                    "STRING" if pa.types.is_string(datatype) else "BOOLEAN" if pa.types.is_boolean(datatype)
                    else "INT" if pa.types.is_integer(datatype) else "DOUBLE" if pa.types.is_floating(datatype)
                    else "ZONED DATETIME"
                )
                types.append({"name": mapping["propertyName"], "type": value})
            graph["nodeTypes"].append({
                "alias": entity["id"], "labels": [entity["name"]], "properties": types,
                "primaryKeyProperties": [properties[key] for key in entity["entityIdParts"]],
            })
            binding = {"nodeTypeAlias": entity["id"], "propertyMappings": mappings}
            kind = "nodeTables"
        elif "/Contextualizations/" in path:
            relation = payloads[f"RelationshipTypes/{path.split('/')[1]}/definition.json"]
            table_id = part["dataBindingTable"]["sourceTableName"]
            graph["edgeTypes"].append({
                "alias": relation["id"], "labels": [relation["name"]], "properties": [],
                "sourceNodeType": {"alias": relation["source"]["entityTypeId"]},
                "destinationNodeType": {"alias": relation["target"]["entityTypeId"]},
            })
            binding = {
                "edgeTypeAlias": relation["id"], "propertyMappings": [], "edgeIdMapping": None,
                "sourceNodeKeyColumns": [item["sourceColumnName"] for item in part["sourceKeyRefBindings"]],
                "destinationNodeKeyColumns": [item["sourceColumnName"] for item in part["targetKeyRefBindings"]],
            }
            kind = "edgeTables"
        else:
            continue
        source_name = f"36_{lakehouse}_3_dbo_{len(table_id)}_{table_id}"
        sources["dataSources"].append({
            "name": source_name, "type": "DeltaTable",
            "properties": {"path": f"abfss://{workspace}@onelake.pbidedicated.windows.net/{lakehouse}/Tables/dbo/{table_id}"},
        })
        binding.update({"id": str(uuid.uuid5(uuid.NAMESPACE_OID, path)), "dataSourceName": source_name})
        bindings[kind].append(binding)
    return _encoded({
        "graphType.json": graph, "graphDefinition.json": bindings, "dataSources.json": sources,
        "stylingConfiguration.json": {
            "$schema": schema + "stylingConfiguration/1.0.0/schema.json",
            "modelLayout": {"positions": {}, "styles": {}, "pan": {"x": 0.0, "y": 0.0}, "zoomLevel": 1.0},
            "visualFormat": None, "scenario": "Ontology",
        },
        "graphSettings.json": {"$schema": p._GRAPH_SCHEMA_ROOT + "graphIndex/definition/graphSettings/1.0.0/schema.json"},
        ".platform": {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": "GraphModel", "displayName": name},
            "config": {"version": "2.0", "logicalId": str(uuid.UUID(int=0))},
        },
    })


@pytest.fixture
def native(compiled):
    compilation, _ = compiled
    ontology = {"parts": p._ontology_parts(compilation, WORKSPACE, LAKEHOUSE, "fixture", "test")}
    definition = _native(compilation, ontology)
    return compilation, ontology, definition


def test_native_all_parts_scalars_and_endpoint_multisets_preserve_sources(native):
    compilation, ontology, definition = native
    before = copy.deepcopy(definition)
    source_proofs = {key: p._table_proof(table) for key, table in compilation.tables.items()}
    checks = p._graph_readback_checks(
        definition, compilation, WORKSPACE, LAKEHOUSE, companion=True, native_ontology=ontology,
    )
    assert checks and any(check.match.startswith("(s:") for check in checks)
    for check in checks:
        for query, expected in check.windows(1):
            assert "__canonical_id" not in query and "__source_entity_id" not in query
            wire = [{k: val.isoformat() if hasattr(val, "isoformat") else val for k, val in row.items()} for row in expected]
            assert p._graph_row_fingerprint(wire, check.fields, from_wire=True) == p._graph_row_fingerprint(
                expected, check.fields, from_wire=False,
            )
            if expected:
                assert p._graph_row_fingerprint(wire + wire, check.fields, from_wire=True) != p._graph_row_fingerprint(
                    expected, check.fields, from_wire=False,
                )
    assert definition == before
    assert source_proofs == {key: p._table_proof(table) for key, table in compilation.tables.items()}


@pytest.mark.parametrize("mutation", [
    "host", "suffix-host", "scheme", "workspace", "lakehouse", "guid", "schema", "table", "traversal",
    "encoding", "query", "fragment", "port", "password", "backslash", "newline", "extra-slash",
    "source-type", "extra-property", "reference", "duplicate", "unused",
])
def test_native_sources_reject_unsafe_or_foreign_paths(native, mutation):
    compilation, ontology, definition = native
    payloads = _decoded(definition)
    source = payloads["dataSources.json"]["dataSources"][0]
    path = source["properties"]["path"]
    edits = {
        "host": path.replace("onelake.pbidedicated.windows.net", "onelake.dfs.fabric.microsoft.com"),
        "suffix-host": path.replace(".net/", ".net.evil/"), "scheme": path.replace("abfss:", "https:"),
        "workspace": path.replace(WORKSPACE, COMPANION), "lakehouse": path.replace(LAKEHOUSE, COMPANION),
        "guid": path.replace(WORKSPACE, "1234"), "schema": path.replace("/dbo/", "/other/"),
        "table": path + "_foreign", "traversal": path.replace("/dbo/", "/dbo/../dbo/"),
        "encoding": path.replace("/Tables/", "/%54ables/"), "query": path + "?x=y", "fragment": path + "#x",
        "port": path.replace(".net/", ".net:443/"), "password": path.replace("@", ":secret@"),
        "backslash": path.replace("/Tables/", "\\Tables/"), "newline": path + "\n",
        "extra-slash": path.replace("/Tables/", "//Tables/"),
    }
    if mutation in edits:
        source["properties"]["path"] = edits[mutation]
    elif mutation == "source-type":
        source["type"] = "Arbitrary"
    elif mutation == "extra-property":
        source["properties"]["filter"] = "ignored"
    elif mutation == "reference":
        payloads["dataSources.json"]["itemReferences"] = []
    elif mutation == "duplicate":
        payloads["dataSources.json"]["dataSources"].append(copy.deepcopy(source))
    else:
        extra = copy.deepcopy(source)
        extra["name"] += "_unused"
        payloads["dataSources.json"]["dataSources"].append(extra)
    with pytest.raises((ValueError, KeyError)):
        p._graph_readback_checks(
            _encoded(payloads), compilation, WORKSPACE, LAKEHOUSE, companion=True, native_ontology=ontology,
        )


@pytest.mark.parametrize("mutation", [
    "part-schema", "extra-part", "extra-setting", "scenario", "styling", "boolean-layout", "platform-name",
    "platform-type", "platform-id", "platform-extra", "duplicate-platform", "mapping",
    "node-key", "node-label", "node-type", "endpoint-type", "endpoint-key", "edge-id",
    "filter", "duplicate-type", "extra-field", "drop-node", "drop-edge",
])
def test_native_all_parts_reject_meaningful_or_unknown_drift(native, mutation):
    compilation, ontology, definition = native
    payloads = _decoded(definition)
    graph, bindings = payloads["graphType.json"], payloads["graphDefinition.json"]
    if mutation == "part-schema":
        graph["$schema"] = graph["$schema"].replace("/1.0.0/", "/2.0.0/")
    elif mutation == "extra-part":
        payloads["unknown.json"] = {}
    elif mutation == "extra-setting":
        payloads["graphSettings.json"]["ignored"] = None
    elif mutation in ("scenario", "styling"):
        payloads["stylingConfiguration.json"]["scenario" if mutation == "scenario" else "visualFormat"] = "other"
    elif mutation == "boolean-layout":
        payloads["stylingConfiguration.json"]["modelLayout"]["zoomLevel"] = True
    elif mutation.startswith("platform-"):
        envelope = payloads[".platform"]
        if mutation == "platform-name":
            envelope["metadata"]["displayName"] = "foreign"
        elif mutation == "platform-type":
            envelope["metadata"]["type"] = "Lakehouse"
        elif mutation == "platform-id":
            envelope["config"]["logicalId"] = "not-a-guid"
        else:
            envelope["config"]["ignored"] = None
    elif mutation == "mapping":
        bindings["nodeTables"][0]["propertyMappings"][0]["sourceColumn"] = "__label"
    elif mutation == "node-key":
        graph["nodeTypes"][0]["primaryKeyProperties"] = ["label"]
    elif mutation == "node-label":
        graph["nodeTypes"][0]["labels"] = ["other"]
    elif mutation == "node-type":
        graph["nodeTypes"][0]["properties"][0]["type"] = "INT"
    elif mutation == "endpoint-type":
        graph["edgeTypes"][0]["destinationNodeType"]["alias"] = "missing"
    elif mutation == "endpoint-key":
        bindings["edgeTables"][0]["sourceNodeKeyColumns"] = ["__canonical_id"]
    elif mutation == "edge-id":
        bindings["edgeTables"][0]["edgeIdMapping"] = {}
    elif mutation == "filter":
        bindings["nodeTables"][0]["filter"] = {}
    elif mutation == "duplicate-type":
        graph["nodeTypes"].append(copy.deepcopy(graph["nodeTypes"][0]))
    elif mutation == "extra-field":
        graph["nodeTypes"][0]["ignored"] = None
    elif mutation == "drop-node":
        bindings["nodeTables"].pop()
    elif mutation == "drop-edge":
        bindings["edgeTables"].pop()
    changed = _encoded(payloads)
    if mutation == "duplicate-platform":
        changed["parts"].append(copy.deepcopy(changed["parts"][-1]))
    with pytest.raises((ValueError, KeyError)):
        p._validate_companion_parts(changed, {"type": "GraphModel", "displayName": "fixture_companion"})
        p._graph_readback_checks(
            changed, compilation, WORKSPACE, LAKEHOUSE, companion=True, native_ontology=ontology,
        )


@pytest.fixture
def retained(owned, monkeypatch, tmp_path):
    case = owned
    _accept(case)
    original = p._read_json(case.kwargs["journal_path"])
    ids = {key: original["actions"][f"create:{key}"]["item_id"] for key in ("lakehouse", "ontology", "graph")}
    compilation = p._compile(case.kwargs["l4_run"], case.kwargs["l3_root"], WORKSPACE, "owned")
    ontology = case.backend.definitions[ids["ontology"]]
    name = "managed_ontology_graph_" + ids["ontology"].replace("-", "")
    native = _native(compilation, ontology, lakehouse=ids["lakehouse"], name=name)
    case.backend.definitions[COMPANION] = native
    case.backend.items[COMPANION] = {"id": COMPANION, "type": "GraphModel", "workspaceId": WORKSPACE, "displayName": name}
    responses = {}
    for graph_id, definition in ((ids["graph"], case.backend.definitions[ids["graph"]]), (COMPANION, native)):
        checks = p._graph_readback_checks(
            definition, compilation, WORKSPACE, ids["lakehouse"],
            companion=graph_id == COMPANION, native_ontology=ontology,
        )
        for kind, prefix, query in (
            ("nodes", "(n:", "MATCH (n) RETURN count(*) AS observed_count"),
            ("edges", "(s:", "MATCH ()-[e]->() RETURN count(*) AS observed_count"),
        ):
            responses[graph_id, query] = [{"observed_count": sum(len(check.rows) for check in checks if check.match.startswith(prefix))}]
        for check in checks:
            for query, expected in check.windows(case.plan["graph_readback_policy"]["page_size"]):
                responses[graph_id, query] = [
                    {key: val.isoformat() if hasattr(val, "isoformat") else val for key, val in row.items()}
                    for row in expected
                ]
    backend_request = case.backend.request

    def request(self, method, url, **kwargs):
        if "/executeQuery" in url:
            case.backend.calls.append((method, url, kwargs["json"]))
            graph_id = url.split("/graphModels/")[1].split("/")[0]
            return _Response({"status": {"code": "00000"}, "result": {
                "kind": "TABLE", "data": copy.deepcopy(responses[graph_id, kwargs["json"]["query"]]),
            }})
        return backend_request(method, url, **kwargs)

    monkeypatch.setattr(p._Run, "request", request)
    monkeypatch.setattr(p._Run, "graph_counts", COUNTS)
    monkeypatch.setattr(p._Run, "graph_content", CONTENT)
    monkeypatch.setattr(p._Run, "companion_readiness", lambda *args, **kwargs: False)
    p.publish_schema2_prototype(**case.kwargs, dry_run=False, approve_live=case.plan["plan_hash"])
    monkeypatch.setattr(p._Run, "companion_readiness", READINESS)
    journal = p._read_json(case.kwargs["journal_path"])
    journal["companion_readback_errors"] = {COMPANION: {"detail": "Companion linkage is not proved by native sources"}}
    p._atomic_json(case.kwargs["journal_path"], journal)
    monkeypatch.setattr(p, "_compiler_hash", lambda: "companion-runtime")
    snapshot = tmp_path / "companion-native.json"
    p._atomic_json(snapshot, {"definition": native})
    case.verify = {
        key: case.recovery[key] for key in ("plan_path", "journal_path", "materialize", "l4_run", "l3_root")
    } | {
        "companion_id": COMPANION, "companion_definition_path": snapshot,
        "verification_plan_path": tmp_path / "verification-plan.json", "proof_path": tmp_path / "proof.json",
    }
    case.responses = responses
    case.native = native
    case.ids = ids
    case.backend.calls.clear()
    return case


def test_public_cli_separate_plan_and_complete_readonly_proof(retained):
    case = retained
    before = {path: path.read_bytes() for path in (
        case.verify["plan_path"], case.verify["journal_path"], case.verify["companion_definition_path"],
    )}
    artifacts = r._artifact_digests(case.verify["materialize"])
    flags = ["app", "verify-prototype-companion"]
    aliases = {
        "plan_path": "plan", "journal_path": "prototype-journal", "companion_definition_path": "companion-definition",
        "verification_plan_path": "verification-plan", "proof_path": "proof",
    }
    for key, value in case.verify.items():
        flags += ["--" + aliases.get(key, key.replace("_", "-")), str(value)]
    runner = CliRunner()
    preview = runner.invoke(cli, flags)
    assert preview.exit_code == 0, preview.output
    plan = json.loads(preview.output)
    assert not case.backend.calls
    assert plan["prior_runtime_repair"]["review"]["current_compiler_hash"] == "repaired-runtime"
    assert plan["current_compiler_hash"] == "companion-runtime"
    assert plan["graph_readback_policy"] == case.plan["graph_readback_policy"]
    actual = runner.invoke(cli, flags + ["--approve-readback", plan["verification_plan_hash"]])
    assert actual.exit_code == 0, actual.output
    proof = p._read_json(case.verify["proof_path"])
    assert proof["status"] == "independent-and-companion-scalars-topology-verified"
    assert proof["historical_journal_errors"]["status"] == "partial"
    assert proof["historical_journal_errors"]["last_error"]
    assert proof["historical_journal_errors"]["companion_readback_errors"]
    assert proof["responses"]
    assert proof["proof_hash"] == canonical_sha256({k: val for k, val in proof.items() if k != "proof_hash"})
    assert proof["item_proofs"]["companion"]["native_definition"] == case.native
    for graph_id in (case.ids["graph"], COMPANION):
        assert proof["content"][graph_id]["status"] == "verified"
        assert all(check["expected_hash"] == check["observed_hash"] for check in proof["content"][graph_id]["checks"])
    assert all(path.read_bytes() == data for path, data in before.items())
    assert r._artifact_digests(case.verify["materialize"]) == artifacts
    assert all(method == "GET" or method == "POST" and (
        url.endswith("/getDefinition") or "/executeQuery?beta=true" in url
    ) for method, url, _ in case.backend.calls)
    with pytest.raises(p.PrototypePublicationError, match="receipt/compiler"):
        p.publish_schema2_prototype(**case.kwargs, dry_run=True, approve_live=None)


@pytest.mark.parametrize("mutation", ["approval", "journal", "receipt", "ownership", "artifact", "snapshot", "compiler", "output"])
def test_separate_proof_rejects_unapproved_local_drift_before_cloud(retained, monkeypatch, mutation):
    case = retained
    plan = v.verify_prototype_companion(**case.verify)
    approval = plan["verification_plan_hash"]
    if mutation == "approval":
        approval = "not-approved"
    elif mutation in ("journal", "receipt", "ownership"):
        journal = p._read_json(case.verify["journal_path"])
        if mutation == "journal":
            journal["last_error"]["message"] = "changed"
        elif mutation == "receipt":
            journal["runtime_repair"]["actor"] = ""
        else:
            journal["actions"]["create:ontology"]["ownership"] = "operator-reconciled"
        p._atomic_json(case.verify["journal_path"], journal)
    elif mutation == "artifact":
        path = case.verify["materialize"] / "native-bound" / "graph.json"
        path.write_text(path.read_text() + "\n")
    elif mutation == "snapshot":
        path = case.verify["companion_definition_path"]
        path.write_text(path.read_text() + "\n")
    elif mutation == "compiler":
        monkeypatch.setattr(p, "_compiler_hash", lambda: "unreviewed-runtime")
    else:
        case.verify["proof_path"] = case.verify["journal_path"]
    with pytest.raises((ValueError, KeyError)):
        v.verify_prototype_companion(**case.verify, approve_readback=approval)
    assert not case.backend.calls


@pytest.mark.parametrize("mutation", ["scalar", "endpoint", "duplicate", "duplicate-edge", "missing", "count", "definition", "metadata", "operation"])
def test_live_mismatches_fail_closed_preserving_journal_and_raw_proof(retained, mutation):
    case = retained
    plan = v.verify_prototype_companion(**case.verify)
    journal = case.verify["journal_path"].read_bytes()
    if mutation in ("scalar", "endpoint", "duplicate", "duplicate-edge", "missing"):
        match = "MATCH (s:" if mutation in ("endpoint", "duplicate-edge") else "MATCH (n:"
        rows = next(rows for (graph_id, query), rows in case.responses.items()
                    if graph_id == COMPANION and query.startswith(match) and rows)
        if mutation in ("duplicate", "duplicate-edge"):
            rows.append(copy.deepcopy(rows[0]))
        elif mutation == "missing":
            rows.pop()
        else:
            rows[0]["c0"] = "entity:wrong"
    elif mutation == "count":
        case.responses[COMPANION, "MATCH (n) RETURN count(*) AS observed_count"][0]["observed_count"] = True
    elif mutation == "definition":
        definition = _decoded(case.native)
        definition["stylingConfiguration.json"]["scenario"] = "changed"
        case.backend.definitions[COMPANION] = _encoded(definition)
    elif mutation == "metadata":
        case.backend.items[COMPANION]["workspaceId"] = LAKEHOUSE
    else:
        case.backend.operation_status = "Failed"
    with pytest.raises((ValueError, KeyError)):
        v.verify_prototype_companion(**case.verify, approve_readback=plan["verification_plan_hash"])
    assert case.verify["journal_path"].read_bytes() == journal
    proof = p._read_json(case.verify["proof_path"])
    assert proof["status"] == "failed-read-only" and proof["responses"]


def test_managed_companion_readiness_uses_native_adapter_without_mutation(retained):
    case = retained
    plan, compilation, _ = v._prepare(*(
        case.verify[key] for key in (
            "plan_path", "journal_path", "materialize", "l4_run", "l3_root",
            "companion_id", "companion_definition_path",
        )
    ))
    run = v._VerificationRun(case.verify["journal_path"], case.plan, plan)
    ontology = compilation.definitions["ontology"]
    assert run.companion_readiness(
        ontology_id=case.ids["ontology"], lakehouse_id=case.ids["lakehouse"], compilation=compilation,
        bound_tables={item["physical_table_id"] for item in ontology["entity_types"] + ontology["relationship_types"]},
        expected=plan["expected"]["companion"],
    )
    assert run.data["verified_service_companions"][COMPANION]["readiness"] == "scalars-and-endpoint-pairs-verified"


def test_readonly_guard_refuses_mutations_unplanned_queries_and_missing_journal(retained, tmp_path):
    case = retained
    plan = v.verify_prototype_companion(**case.verify)
    missing = tmp_path / "missing-journal.json"
    with pytest.raises(FileNotFoundError):
        v._VerificationRun(missing, case.plan, plan)
    assert not missing.exists()
    run = v._VerificationRun(case.verify["journal_path"], case.plan, plan)
    prefix = f"{p.API}/workspaces/{WORKSPACE}/graphModels/{COMPANION}"
    for method, url, body in (
        ("DELETE", prefix, None), ("PATCH", prefix, {"displayName": "changed"}),
        ("POST", prefix + "/jobs/instances", {}),
        ("POST", prefix + "/executeQuery?beta=true", {"query": "DELETE n"}),
        ("POST", prefix.replace(COMPANION, LAKEHOUSE) + "/executeQuery?beta=true",
         {"query": "MATCH (n) RETURN count(*) AS observed_count"}),
    ):
        with pytest.raises(p.PrototypePublicationError):
            run.request(method, url, json=body)
    assert not case.backend.calls


def test_readonly_proof_supports_validated_cursor_pages(retained, monkeypatch):
    case = retained
    plan = v.verify_prototype_companion(**case.verify)
    original = p._Run.request
    paged = []

    def request(self, method, url, **kwargs):
        if f"/{COMPANION}/executeQuery" in url and kwargs["json"]["query"].startswith("MATCH (n:"):
            if "continuationToken=" not in url:
                paged.append(url)
                return _Response({
                    "status": {"code": "00000"}, "result": {"kind": "TABLE", "data": []},
                    "continuationToken": "opaque+token/%=",
                })
            assert "continuationToken=opaque%2Btoken%2F%25%3D" in url
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(p._Run, "request", request)
    result = v.verify_prototype_companion(**case.verify, approve_readback=plan["verification_plan_hash"])
    assert paged
    assert result["status"] == "independent-and-companion-scalars-topology-verified"
    assert any(len(check["pages"]) == 2 for check in result["content"][COMPANION]["checks"])


@pytest.mark.parametrize("mutation", ["metadata", "journal"])
def test_readonly_final_race_check_does_not_issue_success(retained, monkeypatch, mutation):
    case = retained
    plan = v.verify_prototype_companion(**case.verify)
    original = p._Run.request
    changed = []

    def request(self, method, url, **kwargs):
        result = original(self, method, url, **kwargs)
        if f"/{COMPANION}/executeQuery" in url and not changed:
            changed.append(True)
            if mutation == "metadata":
                case.backend.items[COMPANION]["description"] = "changed concurrently"
            else:
                journal = p._read_json(case.verify["journal_path"])
                journal["last_error"]["message"] = "external change"
                p._atomic_json(case.verify["journal_path"], journal)
        return result

    monkeypatch.setattr(p._Run, "request", request)
    with pytest.raises(p.PrototypePublicationError, match="changed during verification"):
        v.verify_prototype_companion(**case.verify, approve_readback=plan["verification_plan_hash"])
    assert changed
    assert p._read_json(case.verify["proof_path"])["status"] == "failed-read-only"


def test_empty_lro_response_bytes_and_request_budget_are_preserved(retained, monkeypatch):
    case = retained
    plan = v.verify_prototype_companion(**case.verify)
    original = copy.deepcopy(case.plan)
    original["graph_readback_policy"]["max_requests"] = 1
    run = v._VerificationRun(case.verify["journal_path"], original, plan)
    url = f"{p.API}/workspaces/{WORKSPACE}/graphModels/{COMPANION}/executeQuery?beta=true"
    run.request("POST", url, json={"query": "MATCH (n) RETURN count(*) AS observed_count"})
    with pytest.raises(p.PrototypePublicationError, match="request cap"):
        run.request("POST", url, json={"query": "MATCH (n) RETURN count(*) AS observed_count"})

    class Empty(_Response):
        def __init__(self):
            super().__init__(None, 202, {"x-ms-operation-id": case.backend.operation_id})
            self.content = b""

        def json(self):
            raise ValueError("empty HTTP 202")

    monkeypatch.setattr(p._Run, "request", lambda *args, **kwargs: Empty())
    response = run.request("POST", url.split("/executeQuery")[0] + "/getDefinition")
    assert response.status_code == 202
    assert run.responses[-1]["body"] is None
    assert run.responses[-1]["raw_body_base64"] == ""
    assert run.responses[-1]["raw_body_hash"] == r._digest(b"")
