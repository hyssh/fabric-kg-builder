"""Bounded probe aliases and native source ownership, entirely offline."""

import copy
import json
import time
from types import SimpleNamespace

import pyarrow.dataset as ds
import pytest

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.deploy import schema2_prototype as publication
from fabric_kg_builder.deploy import schema2_prototype_query as probe
from tests.unit.test_business_quality import _derived_sealed_inputs, _prototype_args
from tests.unit.test_schema2_companion_verification import _native


@pytest.fixture
def query_schema():
    return {
        "nodes": {
            name: {"table_id": name.lower(), "primary_key": "id",
                   "properties": {"id": "__canonical_id", "label": "__label"}}
            for name in ("Record", "Subject")
        },
        "edges": {"HasSubject": {
            "source_label": "Record", "target_label": "Subject",
            "properties": {"id": "__canonical_id"},
        }},
    }


def test_generated_aliases_are_valid_unique_and_reader_keys_follow_them(query_schema):
    query = probe._Query(
        "MATCH (r:Record)-[e:HasSubject]->(s:Subject) "
        "RETURN r.label AS recordName, s.label AS subjectName LIMIT 10",
        query_schema, 10,
    )
    assert query.identity_columns == {"r": "fkg_probe_entity_0", "s": "fkg_probe_entity_1"}
    assert set(query.identity_columns.values()).isdisjoint(query.outputs)
    assert "__probe_" not in query.gql
    for alias in query.identity_columns.values():
        assert " AS " + publication._quote_graph_identifier(alias) in query.gql
    assert query.gql.endswith("LIMIT 11")
    assert query.count_gql.endswith("RETURN count(*) AS observed_count")
    assert probe._Query(
        "MATCH (r:Record) RETURN r.label AS probe_entity_0 LIMIT 1", query_schema, 1,
    ).outputs == {"probe_entity_0": ("r", "label")}


@pytest.mark.parametrize("alias", ["fkg_probe_entity_0", "FKG_PROBE_ENTITY_0", "fkg_probe_custom"])
def test_user_cannot_claim_generated_alias_namespace(query_schema, alias):
    with pytest.raises(publication.PrototypePublicationError, match="Duplicate/reserved"):
        probe._Query(f"MATCH (r:Record) RETURN r.label AS {alias} LIMIT 1", query_schema, 1)


@pytest.mark.parametrize("text", [
    "MATCH (r:Record) RETURN r.label AS __probe_entity_0 LIMIT 1",
    "MATCH (_r:Record) RETURN _r.label AS name LIMIT 1",
    "MATCH (r:Record) RETURN r.label AS name LIMIT 1; DELETE r",
    "MATCH (r:Record) RETURN r.label AS name LIMIT 1 // comment",
    "MATCH (r:Record) RETURN r.unknown AS name LIMIT 1",
    "MATCH (r:Record), (s:Subject) RETURN r.label AS name LIMIT 1",
    "MATCH (r:Record) RETURN r.label AS name LIMIT 11",
    "MATCH (r:Record) RETURN r.label AS name LIMIT 0",
])
def test_alias_repair_does_not_relax_untrusted_query_grammar(query_schema, text):
    with pytest.raises(publication.PrototypePublicationError):
        probe._Query(text, query_schema, 10)


@pytest.fixture
def owned_probe(tmp_path, monkeypatch):
    inputs, quality_policy, l3 = _derived_sealed_inputs(tmp_path, monkeypatch)
    candidate = publication.publish_schema2_prototype(
        **_prototype_args(tmp_path / "candidate", inputs, l3), quality_policy=quality_policy,
    )
    options = _prototype_args(tmp_path / "approved", inputs, l3)
    plan = publication.publish_schema2_prototype(
        **options, quality_policy=quality_policy, approved_limitations=candidate["limitations"],
    )
    assert not plan["blockers"]
    compiled = publication._compile_from_plan(inputs["source"].root, l3, plan)
    ids = {
        "lakehouse": "22222222-2222-4222-8222-222222222222",
        "ontology": "33333333-3333-4333-8333-333333333333",
        "graph": "44444444-4444-4444-8444-444444444444",
        "companion": "55555555-5555-4555-8555-555555555555",
    }
    native, _ = publication._native_definitions(
        compiled, workspace_id=plan["workspace_id"], lakehouse_id=ids["lakehouse"],
        names=plan["names"], description=plan["description"], root=options["materialize_dir"],
        semantic_model=False,
    )
    companion = _native(compiled, native["ontology"], workspace=plan["workspace_id"], lakehouse=ids["lakehouse"])
    bound = options["materialize_dir"] / "native-bound"
    bound.mkdir(exist_ok=True)
    actions = {}
    metadata = {}
    for kind in ("lakehouse", "ontology", "graph"):
        metadata[ids[kind]] = {
            "id": ids[kind], "displayName": plan["names"][kind],
            "type": {"lakehouse": "Lakehouse", "ontology": "Ontology", "graph": "GraphModel"}[kind],
            **({"properties": {"defaultSchema": "dbo"}} if kind == "lakehouse" else {}),
        }
        request = {"displayName": plan["names"][kind], "description": plan["description"]}
        if kind == "lakehouse":
            request["creationPayload"] = {"enableSchemas": True}
        else:
            request["definition"] = native[kind]
            (bound / f"{kind}.json").write_text(canonical_json(native[kind]))
        actions[f"create:{kind}"] = {
            "status": "identity-verified", "item_id": ids[kind], "returned_item_id": ids[kind],
            "metadata": metadata[ids[kind]], "http_status": 201,
            "request_hash": canonical_sha256(request),
            **({"definition_readback_hash": canonical_sha256(publication._definition_payloads(native[kind]))}
               if kind != "lakehouse" else {}),
        }
    metadata[ids["companion"]] = {"id": ids["companion"], "type": "GraphModel", "displayName": "fixture_companion"}
    for name, table in compiled.tables.items():
        actions[f"delta:{name}"] = {
            "status": "verified", "table_proof": publication._table_proof(table),
            "path": f"abfss://{plan['workspace_id']}@onelake.dfs.fabric.microsoft.com/{ids['lakehouse']}/Tables/dbo/{name}",
        }
    journal = {
        "journal_version": publication.FORMAT_VERSION, "plan_hash": plan["plan_hash"],
        "run_id": plan["run_id"], "workspace_id": plan["workspace_id"],
        "policy": "create-only-retain-partial", "actions": actions,
        "verified_service_companions": {ids["companion"]: {
            "ontology_id": ids["ontology"], "lakehouse_id": ids["lakehouse"],
            "definition_readback_hash": canonical_sha256(publication._definition_payloads(companion)),
        }},
    }
    options["journal_path"].write_text(canonical_json(journal))
    return SimpleNamespace(
        inputs=inputs, l3=l3, options=options, plan=plan, compiled=compiled, ids=ids,
        native=native, companion=companion, metadata=metadata, journal=journal,
    )


@pytest.mark.parametrize("repair_policy", [None, "operator-reconciled-create-and-exact-runtime-repair-v1"])
def test_probe_rejects_unapproved_or_reclassified_runtime(owned_probe, monkeypatch, tmp_path, repair_policy):
    case = owned_probe
    monkeypatch.setattr(probe, "_compiler_hash", lambda: "changed-runtime")
    if repair_policy:
        case.journal["runtime_repair"] = {"policy": repair_policy}
        case.options["journal_path"].write_text(canonical_json(case.journal))
    with pytest.raises(publication.PrototypePublicationError, match="accepted returned-ID runtime repair"):
        probe.query_schema2_prototype(
            journal_path=case.options["journal_path"], plan_path=case.options["plan_path"],
            materialize_dir=case.options["materialize_dir"], l4_run=case.inputs["source"].root,
            l3_root=case.l3, questions_path=tmp_path / "unused-questions.json",
            output=tmp_path / "unused-report.json", target="independent-graph",
            live=True, acknowledge_beta=True,
        )


@pytest.mark.parametrize("fail_at", ["bindings", "artifacts", "fresh-proof"])
def test_probe_repaired_runtime_requires_each_existing_guard(owned_probe, monkeypatch, tmp_path, fail_at):
    from fabric_kg_builder.deploy import schema2_prototype_reconcile as reconciliation

    case = owned_probe
    monkeypatch.setattr(probe, "_compiler_hash", lambda: "changed-runtime")
    case.journal["runtime_repair"] = {"policy": reconciliation.RETURNED_ID_POLICY}
    case.options["journal_path"].write_text(canonical_json(case.journal))
    calls = []
    monkeypatch.setattr(reconciliation, "recompile_plan", lambda *args: case.plan)

    def check(name):
        def guard(*args, **kwargs):
            calls.append(name)
            if name == fail_at:
                raise publication.PrototypePublicationError("guard failed: " + name)
        return guard

    monkeypatch.setattr(reconciliation, "validate_runtime_repair", check("bindings"))
    monkeypatch.setattr(reconciliation, "validate_returned_artifacts", check("artifacts"))
    monkeypatch.setattr(reconciliation, "validate_returned_resume", check("fresh-proof"))
    with pytest.raises(publication.PrototypePublicationError, match="guard failed: " + fail_at):
        probe.query_schema2_prototype(
            journal_path=case.options["journal_path"], plan_path=case.options["plan_path"],
            materialize_dir=case.options["materialize_dir"], l4_run=case.inputs["source"].root,
            l3_root=case.l3, questions_path=tmp_path / "unused-questions.json",
            output=tmp_path / "unused-report.json", target="independent-graph",
            live=True, acknowledge_beta=True,
        )
    assert calls == ["bindings", "artifacts", "fresh-proof"][:calls.index(fail_at) + 1]


@pytest.mark.parametrize("target", ["independent-graph", "ontology-companion"])
@pytest.mark.parametrize("legacy_result_alias", [False, True])
def test_complete_probe_reads_generated_aliases_and_derived_citations(
    tmp_path, monkeypatch, owned_probe, target, legacy_result_alias,
):
    """Exercise actual preflight, parsers, ownership, scalar readers and citations.

    Only external Graph/Delta transport and credentials are replaced with local
    fixture responses; these tests do not attest any deployed service.
    """
    case = owned_probe
    is_companion = target == "ontology-companion"
    graph = case.companion if is_companion else case.native["graph"]
    graph_id = case.ids["companion" if is_companion else "graph"]
    schema = probe._schema(
        graph, case.compiled, workspace_id=case.plan["workspace_id"],
        lakehouse_id=case.ids["lakehouse"], companion=is_companion,
    )
    label, node = next((label, node) for label, node in schema["nodes"].items() if any(
        row.get("__label") == "governed record (qualified)"
        for row in case.compiled.tables[node["table_id"]].to_pylist()
    ))
    property_name = next(key for key, column in node["properties"].items() if column == "__label")
    text = f"MATCH (n:{label}) RETURN n.{property_name} AS display LIMIT 10"
    query = probe._Query(text, schema, 10)
    entity_id = case.compiled.tables[node["table_id"]]["__canonical_id"].to_pylist()[0]
    questions = tmp_path / "questions.json"
    questions.write_text(canonical_json({
        "contract_version": "1.0.0", "questions": [{
            "question_id": f"q{i}", "question": f"Fixture display probe {i}", "query": text,
            "expected": {"exact_rows": [{"display": "governed record (qualified)"}]},
        } for i in range(6)],
    }))
    checks = publication._graph_readback_checks(
        graph, case.compiled, case.plan["workspace_id"], case.ids["lakehouse"],
        companion=is_companion, native_ontology=case.native["ontology"],
    )
    responses = {
        gql: rows for check in checks for gql, rows in check.windows(case.plan["graph_readback_policy"]["page_size"])
    }
    responses.update({
        "MATCH (n) RETURN count(*) AS observed_count": [{
            "observed_count": sum(case.compiled.tables[n["table_id"]].num_rows for n in schema["nodes"].values()),
        }],
        "MATCH ()-[e]->() RETURN count(*) AS observed_count": [{
            "observed_count": sum(case.compiled.tables[e["table_id"]].num_rows for e in schema["edges"].values()),
        }],
        query.count_gql: [{"observed_count": 1}],
        query.gql: [{
            "display": "governed record (qualified)",
            "__probe_entity_0" if legacy_result_alias else query.identity_columns["n"]: entity_id,
        }],
    })
    calls = []

    class OfflineProbe(probe._Probe):
        def __init__(self, output, plan, report, timeout):
            self.path, self.plan, self.data = output, plan, report
            self.deadline = time.monotonic() + timeout
            self.credential = SimpleNamespace(get_token=lambda *_: SimpleNamespace(token="unit-test-token"))

        def save(self):
            self.path.write_text(canonical_json(self.data))

        def token(self):
            return "unit-test-token"

        def checked(self, response):
            return response

        def request(self, method, url, **kwargs):
            calls.append((method, url))
            if method == "GET":
                return case.metadata[url.rsplit("/", 1)[-1]]
            assert method == "POST" and "executeQuery" in url
            return {"status": {"code": "00000"}, "result": {
                "kind": "TABLE", "data": copy.deepcopy(responses[kwargs["json"]["query"]]),
            }}

        def definition(self, kind, item_id):
            assert item_id == (case.ids["ontology"] if kind == "ontology" else graph_id)
            return case.native["ontology"] if kind == "ontology" else graph

    class OfflineDelta:
        def __init__(self, uri, storage_options):
            self.table_id = uri.rsplit("/", 1)[-1]
            assert uri == case.journal["actions"][f"delta:{self.table_id}"]["path"]

        def version(self):
            return 0

        def history(self, count):
            return [{
                "prototype_run_id": case.plan["run_id"], "prototype_plan_hash": case.plan["plan_hash"],
                "prototype_action": f"delta:{self.table_id}",
            }]

        def to_pyarrow_dataset(self):
            return ds.dataset(case.compiled.tables[self.table_id])

    def execute(_workspace, _graph_id, gql, *, transport, **kwargs):
        assert _graph_id == graph_id
        return transport.request("POST", "executeQuery", json={"query": gql})

    monkeypatch.setattr(probe, "_Probe", OfflineProbe)
    monkeypatch.setattr(probe, "execute_gql_query", execute)
    monkeypatch.setattr("deltalake.DeltaTable", OfflineDelta)
    output = tmp_path / "probe.json"
    arguments = {
        "l4_run": case.inputs["source"].root, "l3_root": case.l3,
        "plan_path": case.options["plan_path"], "journal_path": case.options["journal_path"],
        "materialize_dir": case.options["materialize_dir"], "questions_path": questions,
        "output": output, "target": target, "max_rows": 10, "max_pages": 2, "timeout_seconds": 300,
        "live": True, "acknowledge_beta": True,
    }
    if legacy_result_alias:
        with pytest.raises(publication.PrototypePublicationError, match="missing or unexpected columns"):
            probe.query_schema2_prototype(**arguments)
        return
    report = probe.query_schema2_prototype(**arguments)
    assert report["ontology_equivalent"] is is_companion
    assert report["graph_content_readbacks"][graph_id]["status"] == "verified"
    assert len(report["questions"]) == 6
    for result in report["questions"]:
        assert result["identity_columns"] == {"n": "fkg_probe_entity_0"}
        assert result["status"] == "operator-assertions-passed"
        assert result["rows"] == [{"display": "governed record (qualified)", "fkg_probe_entity_0": entity_id}]
        citations = [c for row in result["citations"] for c in row["links"] if c["answer_columns"] == ["display"]]
        assert citations and all(c["claim_kind"] == "derived-instance-display" for c in citations)
    assert calls and all(method == "GET" or "executeQuery" in url for method, url in calls)
    assert json.loads(output.read_text()) == report


def test_native_query_schema_requires_exact_source_ownership(owned_probe):
    case = owned_probe
    with pytest.raises(publication.PrototypePublicationError, match="ownership"):
        probe._schema(case.companion, case.compiled, companion=True)
    with pytest.raises(publication.PrototypePublicationError, match="outside approved"):
        probe._schema(
            case.companion, case.compiled, workspace_id=case.plan["workspace_id"],
            lakehouse_id="66666666-6666-4666-8666-666666666666", companion=True,
        )
