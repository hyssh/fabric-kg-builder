"""Offline public Graph label repair: authority, full content, and mutation guards."""

import base64
import copy
import json
import uuid
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.deploy import schema2_graph_presentation as m
from fabric_kg_builder.deploy import schema2_prototype as p
from tests.unit.test_l5a_structured_publication import _inputs
from tests.unit.test_schema2_prototype_agent import _Backend, _Response, WORKSPACE


def _edit(definition, path, fn):
    result = copy.deepcopy(definition)
    part = next(x for x in result["parts"] if x["path"] == path)
    body = json.loads(base64.b64decode(part["payload"]))
    fn(body)
    part["payload"] = base64.b64encode(canonical_json(body).encode()).decode()
    return result


@pytest.fixture
def graph_case(tmp_path, monkeypatch):
    source = _inputs(tmp_path / "sealed")["source"]
    l3 = tmp_path / "sealed" / ".fkg" / "l3"
    next(l3.rglob("output-manifest.json")).write_text(canonical_json(source.input_manifest.model_dump(mode="json")) + "\n")
    original_compile = p._compile

    def historical(*args, **kwargs):
        compiled = original_compile(*args, **kwargs)
        for part in compiled.graph_parts:
            if part["path"] == "graphType.json":
                for kind in ("nodeTypes", "edgeTypes"):
                    for i, item in enumerate(part["payload_json"][kind]):
                        item["labels"] = [f"Legacy_{kind}_{i}"]
        return compiled

    monkeypatch.setattr(p, "_compile", historical)
    materialize = tmp_path / "materialized"
    plan_path, journal_path = tmp_path / "original-plan.json", tmp_path / "original-journal.json"
    plan = p.publish_schema2_prototype(
        l4_run=source.root, l3_root=l3, workspace_id=WORKSPACE, name_prefix="labels",
        plan_path=plan_path, journal_path=journal_path, materialize_dir=materialize,
        approved_limitations=(p.METADATA_ONLY_ALIASES_LIMITATION,), dry_run=True, approve_live=None,
    )
    assert not plan["blockers"]
    compilation = historical(source.root, l3, WORKSPACE, "labels")
    ids = {kind: str(uuid.uuid4()) for kind in ("lakehouse", "ontology", "graph")}
    definitions, _ = p._native_definitions(
        compilation, workspace_id=WORKSPACE, lakehouse_id=ids["lakehouse"],
        names=plan["names"], description=plan["description"], root=materialize, semantic_model=False,
    )
    (materialize / "native-bound").mkdir(exist_ok=True)
    for kind, value in definitions.items():
        p._atomic_json(materialize / "native-bound" / f"{kind}.json", value, create=True)
    backend = _Backend()
    actions = {}
    for kind, item_id in ids.items():
        metadata = {
            "id": item_id, "workspaceId": WORKSPACE, "type": p.ITEM_TYPES[kind],
            "displayName": plan["names"][kind], "description": plan["description"],
        }
        request = {"displayName": plan["names"][kind], "description": plan["description"]}
        if kind == "lakehouse":
            metadata["properties"] = {"defaultSchema": "dbo"}
            request["creationPayload"] = {"enableSchemas": True}
        else:
            request["definition"] = definitions[kind]
            backend.definitions[item_id] = copy.deepcopy(definitions[kind])
        backend.items[item_id] = metadata
        actions[f"create:{kind}"] = {
            "item_id": item_id, "returned_item_id": item_id, "kind": kind, "status": "identity-verified",
            "http_status": 201, "display_name": plan["names"][kind],
            "request_hash": canonical_sha256(request), "metadata": metadata,
        }
    for table_id, proof in plan["tables"].items():
        actions[f"delta:{table_id}"] = {
            "status": "verified", "delta_version": 0, "table_proof": proof, "readback": proof,
            "path": f"abfss://{WORKSPACE}@onelake.dfs.fabric.microsoft.com/{ids['lakehouse']}/Tables/dbo/{table_id}",
        }
    journal = {
        "journal_version": p.FORMAT_VERSION, "plan_hash": plan["plan_hash"], "run_id": plan["run_id"],
        "workspace_id": WORKSPACE, "actions": actions, "baseline_item_ids": [],
        "graph_content_readbacks": {ids["graph"]: {"status": "verified"}},
    }
    p._atomic_json(journal_path, journal, create=True)
    monkeypatch.setattr(p, "_compile", original_compile)
    compilation = original_compile(source.root, l3, WORKSPACE, "labels")
    updates, mode = [], {"value": "normal"}
    original_request = backend.request

    def request(method, url, **kwargs):
        path = url.split("?", 1)[0]
        if path.endswith("/updateDefinition"):
            assert (tmp_path / "repair" / "update-intent.json").is_file()
            assert kwargs["json"].keys() == {"definition"}
            updates.append(copy.deepcopy(kwargs["json"]))
            if mode["value"] == "lost-before":
                raise TimeoutError("Lost response before apply")
            backend.definitions[ids["graph"]] = copy.deepcopy(kwargs["json"]["definition"])
            if mode["value"] == "lost-after":
                raise TimeoutError("Lost response after apply")
            if mode["value"] == "drift-after":
                backend.definitions[ids["graph"]] = _edit(
                    backend.definitions[ids["graph"]], "graphDefinition.json",
                    lambda x: x["nodeTables"][0].update({"filter": "unexpected"}),
                )
            if mode["value"] == "lro":
                return _Response(None, 202, {
                    "x-ms-operation-id": backend.operation_id,
                    "Location": f"https://wabi-us-north-central-h-primary-redirect.analysis.windows.net/v1/operations/{backend.operation_id}",
                })
            return _Response({})
        if path.endswith("/executeQuery"):
            backend.calls.append((method, url, copy.deepcopy(kwargs["json"])))
            query = kwargs["json"]["query"]
            checks = p._graph_readback_checks(
                backend.definitions[ids["graph"]], compilation, WORKSPACE, ids["lakehouse"], companion=False,
            )
            if "count(*)" in query:
                rows = [{"observed_count": sum(
                    len(c.rows) for c in checks
                    if c.match.startswith("(s:" if "[e]" in query else "(n:")
                )}]
            else:
                candidates = {
                    q: rows for check in checks
                    for q, rows in check.windows(plan["graph_readback_policy"]["page_size"])
                }
                rows = copy.deepcopy(candidates[query])
                if rows and mode["value"] == "corrupt-data":
                    rows[0]["c0"] = "wrong"
            return _Response({"status": {"code": "00000"}, "result": {"kind": "TABLE", "data": rows}})
        return original_request(method, url, **kwargs)

    monkeypatch.setattr(p._Run, "request", lambda self, method, url, **kwargs: request(method, url, **kwargs))
    import azure.identity

    monkeypatch.setattr(azure.identity, "AzureCliCredential", lambda: SimpleNamespace(
        get_token=lambda *_: SimpleNamespace(token="offline"),
    ))
    monkeypatch.setattr(m.o, "_retry", lambda _: None)
    kwargs = {
        "workspace_id": WORKSPACE, "graph_id": ids["graph"], "plan_path": plan_path,
        "journal_path": journal_path, "materialize": materialize, "l4_run": source.root,
        "l3_root": l3, "state": tmp_path / "repair",
    }
    return SimpleNamespace(
        kwargs=kwargs, backend=backend, updates=updates, mode=mode, compilation=compilation,
        ids=ids, original=definitions["graph"], original_bytes=(plan_path.read_bytes(), journal_path.read_bytes()),
    )


def _plan(case):
    return m.repair_graph_labels(**case.kwargs)


def _reviewed_graph(case):
    from tests.unit.test_naming_review import write_review

    context = m._local(**{k: v for k, v in case.kwargs.items() if k != "state"})
    path = case.kwargs["state"].parent / "copilot-names.json"
    payload = write_review(path, context)
    case.kwargs["naming_review"] = path
    return payload


def test_copilot_exact_names_reach_graph_labels_with_immutable_originals(graph_case):
    case = graph_case
    payload = _reviewed_graph(case)
    plan = _plan(case)
    sealed = p._read_json(case.kwargs["state"] / "plan.json")
    assert sealed["local_evidence"]["naming_review"]["payload"] == payload
    assert all(
        item["new_name"] == payload["entity_types" if item["kind"] == "node" else "relationship_types"][
            item["canonical_id"]
        ]["native_name"] and item["reason"] for item in sealed["mapping"]
    )
    result = _apply(case, plan)
    assert result["status"] == "verified-labels-only" and len(case.updates) == 1
    replacement = p._read_json(case.kwargs["state"] / "replacement.json")
    native = m.o._decode(replacement)["graphType.json"]
    assert {item["labels"][0] for item in native["edgeTypes"]} == {
        item["native_name"] for item in payload["relationship_types"].values()
    }
    assert case.original_bytes == (
        case.kwargs["plan_path"].read_bytes(), case.kwargs["journal_path"].read_bytes(),
    )
    assert m.label_diff(case.original, replacement)


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("mutation", ["bytes", "delete", "omit"])
def test_review_drift_blocks_graph_apply_and_resume(graph_case, resume, mutation):
    case = graph_case
    _reviewed_graph(case)
    plan = _plan(case)
    if resume:
        case.mode["value"] = "lost-after"
        with pytest.raises(TimeoutError):
            _apply(case, plan)
        assert len(case.updates) == 1
    path = case.kwargs["naming_review"]
    if mutation == "bytes":
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "delete":
        path.unlink()
    else:
        case.kwargs.pop("naming_review")
    with pytest.raises((ValueError, OSError)):
        _apply(case, plan, resume=resume)
    assert len(case.updates) == int(resume)


def test_reviewed_graph_unknown_outcome_resume_never_reposts(graph_case):
    case = graph_case
    _reviewed_graph(case)
    plan = _plan(case)
    case.mode["value"] = "lost-after"
    with pytest.raises(TimeoutError):
        _apply(case, plan)
    assert _apply(case, plan, resume=True)["status"] == "verified-labels-only"
    assert len(case.updates) == 1


def _apply(case, plan, **kwargs):
    return m.repair_graph_labels(
        **case.kwargs, live=True, approve_plan=plan["plan_hash"], acknowledge_nontransactional=True, **kwargs,
    )


def test_complete_plan_apply_preserves_sources_identity_and_data(graph_case):
    case = graph_case
    plan = _plan(case)
    assert plan["status"] == "planned-read-only"
    assert not case.updates
    replacement = p._read_json(case.kwargs["state"] / "replacement.json")
    changes = m.label_diff(case.original, replacement)
    assert changes and all(x["path"].endswith("/labels") for x in changes)
    result = _apply(case, plan)
    assert result["status"] == "verified-labels-only" and len(case.updates) == 1
    receipt = p._read_json(case.kwargs["state"] / "receipt.json")
    assert receipt["business_fact_approval"] is False
    assert receipt["content"]["content"]["status"] == "verified"
    before = p._read_json(case.kwargs["state"] / "before-content.json")
    assert before["expected"] == receipt["content"]["expected"]
    assert [
        (check["observed_hash"], check["observed_rows"]) for check in before["content"]["checks"]
    ] == [
        (check["observed_hash"], check["observed_rows"]) for check in receipt["content"]["content"]["checks"]
    ]
    assert (case.kwargs["plan_path"].read_bytes(), case.kwargs["journal_path"].read_bytes()) == case.original_bytes
    receipt_bytes = (case.kwargs["state"] / "receipt.json").read_bytes()
    with pytest.raises(m.Error, match="immutable"):
        _apply(case, plan, resume=True)
    assert (case.kwargs["state"] / "receipt.json").read_bytes() == receipt_bytes
    assert len(case.updates) == 1


@pytest.mark.parametrize("path,mutation", [
    ("graphType.json", lambda x: x["nodeTypes"][0].update(alias="changed")),
    ("graphType.json", lambda x: x["nodeTypes"][0]["properties"][0].update(name="changed")),
    ("graphType.json", lambda x: x["nodeTypes"][0].update(primaryKeyProperties=["changed"])),
    ("graphType.json", lambda x: x["edgeTypes"][0]["sourceNodeType"].update(alias="changed")),
    ("graphDefinition.json", lambda x: x["nodeTables"][0].update(dataSourceName="changed")),
    ("dataSources.json", lambda x: x["dataSources"][0]["properties"].update(path="Tables/dbo/other")),
    ("stylingConfiguration.json", lambda x: x.update(unapproved=True)),
])
def test_nonlabel_changes_fail_closed(graph_case, path, mutation):
    with pytest.raises(m.Error):
        m.label_diff(graph_case.original, _edit(graph_case.original, path, mutation))


@pytest.mark.parametrize("mutation", [
    lambda c: c.definitions["ontology"]["presentation_catalog"]["entity_types"].clear(),
    lambda c: c.definitions["ontology"]["presentation_catalog"]["relationship_types"].clear(),
    lambda c: c.definitions["graph"]["node_types"].append(copy.deepcopy(c.definitions["graph"]["node_types"][0])),
    lambda c: c.graph_catalog["edges"].append(copy.deepcopy(c.graph_catalog["edges"][0])),
    lambda c: c.graph_catalog["edges"][0].update(source_table_id="wrong"),
])
def test_missing_catalog_or_ambiguous_crosswalk_fails(graph_case, mutation):
    compilation = copy.deepcopy(graph_case.compilation)
    mutation(compilation)
    with pytest.raises((m.Error, KeyError)):
        m.render_labels(graph_case.original, compilation, WORKSPACE, graph_case.ids["lakehouse"])


def test_catalog_collisions_resolve_deterministically_not_by_prefix(graph_case, monkeypatch):
    original = p.compile_l5a_publication

    def colliding(*args, **kwargs):
        compiled = original(*args, **kwargs)
        catalog = compiled.definitions["ontology"]["presentation_catalog"]
        for kind in ("entity_types", "relationship_types"):
            for value in catalog[kind].values():
                value["display_name"] = "Same approved label"
        return compiled

    monkeypatch.setattr(p, "compile_l5a_publication", colliding)
    compilation = p._compile(graph_case.kwargs["l4_run"], graph_case.kwargs["l3_root"], WORKSPACE, "labels")
    replacement, mapping = m.render_labels(graph_case.original, compilation, WORKSPACE, graph_case.ids["lakehouse"])
    labels = [x["label"].casefold() for x in mapping]
    assert len(set(labels)) == len(labels)
    assert all(label.startswith("same_approved_label") for label in labels)
    assert m.render_labels(graph_case.original, compilation, WORKSPACE, graph_case.ids["lakehouse"])[0] == replacement


@pytest.mark.parametrize("part_path", ["graphType.json", "graphDefinition.json", "dataSources.json"])
def test_compiler_drift_outside_approved_labels_rejected(graph_case, part_path):
    compilation = copy.deepcopy(graph_case.compilation)
    part = next(part for part in compilation.graph_parts if part["path"] == part_path)
    part["payload_json"]["unexpected"] = True
    with pytest.raises(m.Error, match="Compiled graph"):
        m.render_labels(graph_case.original, compilation, WORKSPACE, graph_case.ids["lakehouse"])


def test_compiler_labels_must_match_approved_catalog(graph_case):
    compilation = copy.deepcopy(graph_case.compilation)
    part = next(part for part in compilation.graph_parts if part["path"] == "graphType.json")
    part["payload_json"]["nodeTypes"][0]["labels"] = ["Unapproved"]
    with pytest.raises(m.Error, match="Compiled graph"):
        m.render_labels(graph_case.original, compilation, WORKSPACE, graph_case.ids["lakehouse"])


def test_observed_service_defaults_remain_raw_unchanged(graph_case):
    schema = "https://developer.microsoft.com/json-schemas/fabric/item/graphIndex/definition"
    original = _edit(graph_case.original, "graphDefinition.json", lambda x: [
        edge.update(edgeIdMapping=None) for edge in x["edgeTables"]
    ])
    original = _edit(original, "stylingConfiguration.json", lambda x: x.update(visualFormat=None))
    original["parts"] += p.encode_parts_for_api([{
        "path": "graphSettings.json", "payload_json": {"$schema": schema + "/graphSettings/1.0.0/schema.json"},
    }])
    replacement, _ = m.render_labels(original, graph_case.compilation, WORKSPACE, graph_case.ids["lakehouse"])
    assert m.label_diff(original, replacement)
    assert [x for x in original["parts"] if x["path"] != "graphType.json"] == [
        x for x in replacement["parts"] if x["path"] != "graphType.json"
    ]
    original = _edit(original, "graphSettings.json", lambda x: x.update(unexpected=True))
    with pytest.raises(m.Error, match="Compiled graph"):
        m.render_labels(original, graph_case.compilation, WORKSPACE, graph_case.ids["lakehouse"])


@pytest.mark.parametrize("mutation", ["graph", "catalog", "journal", "plan", "backup", "replacement", "mapping", "data"])
def test_drift_blocks_update(graph_case, monkeypatch, mutation):
    case = graph_case
    plan = _plan(case)
    if mutation == "graph":
        case.backend.definitions[case.ids["graph"]] = _edit(
            case.original, "graphType.json", lambda x: x["nodeTypes"][0].update(labels=["Unexpected"]),
        )
    elif mutation == "catalog":
        original = p._compile

        def altered(*args, **kwargs):
            result = original(*args, **kwargs)
            result.definitions["ontology"]["presentation_catalog"]["entity_types"].clear()
            return result

        monkeypatch.setattr(p, "_compile", altered)
    elif mutation == "journal":
        case.kwargs["journal_path"].write_bytes(case.kwargs["journal_path"].read_bytes() + b" ")
    elif mutation == "data":
        case.mode["value"] = "corrupt-data"
    else:
        file = case.kwargs["state"] / f"{mutation}.json"
        value = p._read_json(file)
        value["unexpected"] = True
        file.write_text(canonical_json(value))
    with pytest.raises((m.Error, KeyError)):
        _apply(case, plan)
    assert not case.updates
    assert not (case.kwargs["state"] / "receipt.json").exists()
    assert list((case.kwargs["state"] / "events").glob("*.json"))


@pytest.mark.parametrize("mode", ["lost-after", "lost-before", "drift-after"])
def test_unknown_outcome_never_reposts_and_retains_failure_proof(graph_case, mode):
    case = graph_case
    plan = _plan(case)
    case.mode["value"] = mode
    with pytest.raises((m.Error, TimeoutError)):
        _apply(case, plan)
    assert len(case.updates) == 1
    assert (case.kwargs["state"] / "update-intent.json").exists()
    with pytest.raises(m.Error, match="intent"):
        _apply(case, plan)
    if mode == "lost-after":
        assert _apply(case, plan, resume=True)["status"] == "verified-labels-only"
    else:
        with pytest.raises(m.Error):
            _apply(case, plan, resume=True)
    assert len(case.updates) == 1


def test_regional_lro_location_polled_only_on_canonical_api(graph_case):
    case = graph_case
    plan = _plan(case)
    case.mode["value"] = "lro"
    assert _apply(case, plan)["status"] == "verified-labels-only"
    assert all(url.startswith(p.API) for _, url, _ in case.backend.calls)


def test_managed_graph_and_wrong_approval_cannot_update(graph_case):
    case = graph_case
    with pytest.raises(m.Error, match="independent"):
        m.repair_graph_labels(**{**case.kwargs, "graph_id": case.ids["ontology"]})
    plan = _plan(case)
    with pytest.raises(m.Error, match="Exact"):
        _apply(case, {**plan, "plan_hash": "0" * 64})
    with pytest.raises(m.Error, match="Resume"):
        _apply(case, plan, resume=True)
    assert not case.updates


def test_public_cli_dry_run_and_apply_conflict(graph_case):
    case = graph_case
    flags = {
        "workspace-id": case.kwargs["workspace_id"], "graph-id": case.kwargs["graph_id"],
        "l4-run": case.kwargs["l4_run"], "l3-root": case.kwargs["l3_root"],
        "publication-plan": case.kwargs["plan_path"], "prototype-journal": case.kwargs["journal_path"],
        "materialize": case.kwargs["materialize"], "state": case.kwargs["state"],
    }
    args = ["app", "repair-graph-labels"] + [v for k, val in flags.items() for v in (f"--{k}", str(val))]
    runner = CliRunner()
    conflict = runner.invoke(cli, ["--dry-run", *args, "--live"])
    assert conflict.exit_code == 2 and not case.backend.calls
    planned = runner.invoke(cli, [*args, "--dry-run"])
    assert planned.exit_code == 0, planned.output
    assert json.loads(planned.output)["status"] == "planned-read-only"
    assert runner.invoke(cli, args).exit_code != 0
    assert not case.updates


@pytest.mark.parametrize("kind", ["nodeTypes", "edgeTypes"])
def test_label_arrays_names_and_casefold_collisions_rejected(graph_case, kind):
    invalid = _edit(
        graph_case.original, "graphType.json", lambda x: x[kind][0].update(labels=["bad-label"]),
    )
    with pytest.raises(m.Error, match="label"):
        m.label_diff(graph_case.original, invalid)
    invalid = _edit(
        graph_case.original, "graphType.json", lambda x: x[kind][0].update(labels=["One", "Two"]),
    )
    with pytest.raises(m.Error, match="label"):
        m.label_diff(graph_case.original, invalid)
    body = m.o._decode(graph_case.original)["graphType.json"]
    if len(body[kind]) > 1:
        collision = _edit(
            graph_case.original, "graphType.json",
            lambda x: x[kind][1].update(labels=[x[kind][0]["labels"][0].lower()]),
        )
        with pytest.raises(m.Error, match="collision"):
            m.label_diff(graph_case.original, collision)


def test_transport_rejects_other_items_refresh_delete_and_unplanned_queries(graph_case):
    case = graph_case
    run = m._GraphRun(case.kwargs["journal_path"], p._read_json(case.kwargs["plan_path"]), case.ids["graph"])
    run.update_allowed = True
    root = f"{p.API}/workspaces/{WORKSPACE}"
    for method, url, body in [
        ("POST", f"{root}/graphModels", {"displayName": "new"}),
        ("DELETE", f"{root}/graphModels/{case.ids['graph']}", None),
        ("POST", f"{root}/graphModels/{case.ids['graph']}/jobs/instances?jobType=Refresh", {}),
        ("POST", f"{root}/ontologies/{case.ids['ontology']}/updateDefinition", {}),
        ("POST", f"{root}/graphModels/{case.ids['graph']}/executeQuery?beta=true", {"query": "MATCH (n) DELETE n"}),
        ("GET", "https://untrusted.example/operations/" + str(uuid.uuid4()), None),
    ]:
        with pytest.raises(m.Error):
            run.request(method, url, **({"json": body} if body is not None else {}))
    assert not case.updates and not case.backend.calls
