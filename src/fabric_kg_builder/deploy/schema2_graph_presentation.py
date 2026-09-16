"""Separately approved, same-item independent Graph labels-only repair."""

from __future__ import annotations

import base64
import copy
import fcntl
import json
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.deploy import schema2_ontology_presentation as o
from fabric_kg_builder.deploy import schema2_prototype as p
from fabric_kg_builder.deploy import schema2_prototype_reconcile as r
from fabric_kg_builder.deploy.ontology_names import (
    NATIVE_NAME_PATTERN,
    allocate_readable_names,
    readable_catalog_from_domain,
)
from fabric_kg_builder.deploy.naming_review import NamingReview

Error = p.PrototypePublicationError
POLICY = "independent-graph-labels-only-same-item-v1"
NOTICE = (
    "Only nodeTypes/edgeTypes labels on the explicitly owned independent Graph; "
    "no business-fact approval, no create/delete/refresh, no Ontology/managed Graph/"
    "Lakehouse/data/alias/key/property/binding/instance-identity changes"
)


def label_diff(before: dict, after: dict) -> list[dict]:
    """Reject every change except single native labels at stable type positions."""
    old, new = o._decode(before), o._decode(after)
    if set(old) != set(new) or "graphType.json" not in old:
        raise Error("Graph definition part scope changed")
    if [x["path"] for x in before["parts"]] != [x["path"] for x in after["parts"]]:
        raise Error("Graph part order changed")
    for left, right in zip(before["parts"], after["parts"], strict=True):
        if left["path"] != "graphType.json" and left != right:
            raise Error("Non-label graph part changed (including raw source binding bytes)")
    expected = copy.deepcopy(old["graphType.json"])
    changes = []
    for kind in ("nodeTypes", "edgeTypes"):
        left, right = expected[kind], new["graphType.json"][kind]
        if len(left) != len(right):
            raise Error("Graph type scope changed")
        aliases = [item["alias"] for item in left]
        labels = []
        if len(set(aliases)) != len(aliases):
            raise Error("Duplicate graph type alias")
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            if (
                a["alias"] != b["alias"] or not isinstance(a.get("labels"), list)
                or len(a["labels"]) != 1 or not isinstance(b.get("labels"), list)
                or len(b["labels"]) != 1 or not isinstance(b["labels"][0], str)
                or not re.fullmatch(NATIVE_NAME_PATTERN, b["labels"][0])
            ):
                raise Error("Invalid native graph label or changed alias")
            labels.append(b["labels"][0].casefold())
            if a["labels"] != b["labels"]:
                changes.append({
                    "path": f"/graphType.json/{kind}/{index}/labels",
                    "alias": a["alias"], "before": a["labels"], "after": b["labels"],
                })
            a["labels"] = copy.deepcopy(b["labels"])
        if len(set(labels)) != len(labels):
            raise Error("Case-insensitive graph label collision")
    if expected != new["graphType.json"]:
        raise Error("Graph non-label fields changed")
    return changes


def render_labels(
    definition: dict, compilation: Any, workspace_id: str, lakehouse_id: str,
    naming_review: NamingReview | None = None,
) -> tuple[dict, list]:
    """Resolve physical source tables through sealed canonical crosswalks only."""
    graph = compilation.definitions["graph"]
    catalog = compilation.definitions["ontology"]["presentation_catalog"]
    nodes = {n["canonical_semantic_type_id"]: n for n in graph["node_types"]}
    node_tables = {n["physical_table_id"]: key for key, n in nodes.items()}
    if len(nodes) != len(graph["node_types"]) or len(node_tables) != len(nodes):
        raise Error("Ambiguous canonical node/table crosswalk")
    node_names = (
        {key: naming_review.entity_types[key].native_name for key in nodes} if naming_review
        else allocate_readable_names({key: catalog["entity_types"][key] for key in nodes})
    )
    payloads = o._decode(definition)
    sources = p._graph_source_tables(
        payloads["dataSources.json"], workspace_id, lakehouse_id, compilation.tables, companion=False,
    )
    # Reuse the full strict scalar/identity/endpoint/binding guard, not a relaxed
    # name-only parser. It does not require compiler-generated presentation.
    p._graph_readback_checks(definition, compilation, workspace_id, lakehouse_id, companion=False)
    native = copy.deepcopy(payloads["graphType.json"])
    node_bindings = {b["nodeTypeAlias"]: sources[b["dataSourceName"]] for b in payloads["graphDefinition.json"]["nodeTables"]}
    edge_bindings = {b["edgeTypeAlias"]: sources[b["dataSourceName"]] for b in payloads["graphDefinition.json"]["edgeTables"]}
    edge_tables = {edge["physical_table_id"]: edge for edge in compilation.graph_catalog["edges"]}
    if len(edge_tables) != len(compilation.graph_catalog["edges"]):
        raise Error("Ambiguous edge/table crosswalk")
    canonical_edges = {edge["canonical_semantic_relationship_id"]: edge for edge in graph["edge_types"]}
    if len(canonical_edges) != len(graph["edge_types"]):
        raise Error("Duplicate canonical relationship crosswalk")
    edge_metadata, mapping = {}, []
    for item in native["nodeTypes"]:
        table = node_bindings[item["alias"]]
        canonical_id = node_tables[table]
        old_name = item["labels"][0]
        item["labels"] = [node_names[canonical_id]]
        mapping.append({
            "kind": "node", "alias": item["alias"], "physical_table_id": table,
            "canonical_id": canonical_id, "approved": catalog["entity_types"][canonical_id],
            "label": item["labels"][0],
            **({
                "old_name": old_name, "new_name": item["labels"][0],
                "display_name": naming_review.entity_types[canonical_id].display_name,
                "reason": naming_review.entity_types[canonical_id].reason,
            } if naming_review else {}),
        })
    for item in native["edgeTypes"]:
        edge = edge_tables[edge_bindings[item["alias"]]]
        key = edge["canonical_relationship_id"]
        approved = catalog["relationship_types"][key]
        semantic = canonical_edges[key]
        if edge["source_table_id"] != semantic["physical_table_id"]:
            raise Error("Edge source table differs from canonical relationship crosswalk")
        metadata = approved
        if naming_review and (
            naming_review.relationship_types[key].source_type_ids != [edge["source_type"]]
            or naming_review.relationship_types[key].target_type_ids != [edge["target_type"]]
        ):
            raise Error("Reviewed Graph naming requires one canonical relationship per exact typed pair")
        if len(semantic["allowed_source_semantic_type_ids"]) * len(semantic["allowed_target_semantic_type_ids"]) > 1:
            metadata = {"display_name": (
                f"{node_names[edge['source_type']]} {approved['display_name']} {node_names[edge['target_type']]}"
            )}
        edge_metadata[item["alias"]] = metadata
        mapping.append({
            "kind": "edge", "alias": item["alias"], "physical_table_id": edge["physical_table_id"],
            "canonical_id": key, "approved": approved,
            "source_type": edge["source_type"], "target_type": edge["target_type"],
            **({
                "old_name": item["labels"][0], "new_name": naming_review.relationship_types[key].native_name,
                "display_name": naming_review.relationship_types[key].display_name,
                "verb": naming_review.relationship_types[key].verb,
                "source_type_ids": naming_review.relationship_types[key].source_type_ids,
                "target_type_ids": naming_review.relationship_types[key].target_type_ids,
                "reason": naming_review.relationship_types[key].reason,
            } if naming_review else {}),
        })
    edge_names = (
        {item["alias"]: naming_review.relationship_types[item["canonical_id"]].native_name
         for item in mapping if item["kind"] == "edge"} if naming_review
        else allocate_readable_names(edge_metadata, prefix="Relationship", reserved=list(node_names.values()))
    )
    for item in native["edgeTypes"]:
        item["labels"] = [edge_names[item["alias"]]]
    for item in mapping:
        if item["kind"] == "edge":
            item["label"] = edge_names[item["alias"]]
    desired = o._decode({"parts": p.encode_parts_for_api(json.loads(
        json.dumps(compilation.graph_parts).replace(p.LAKEHOUSE_REFERENCE, lakehouse_id)
    ))})
    desired_native = desired["graphType.json"]
    for kind in ("nodeTypes", "edgeTypes"):
        by_alias = {item["alias"]: item for item in desired_native[kind]}
        if (
            len(by_alias) != len(desired_native[kind])
            or set(by_alias) != {item["alias"] for item in native[kind]}
        ):
            raise Error("Compiled graph aliases differ from live graph")
        desired_native[kind] = [by_alias[item["alias"]] for item in native[kind]]
        if naming_review:
            # Only substitute independently reviewed labels; compiled aliases,
            # properties, endpoints and every source part remain strict proofs.
            names = node_names if kind == "nodeTypes" else edge_names
            for item in desired_native[kind]:
                key = node_tables[node_bindings[item["alias"]]] if kind == "nodeTypes" else item["alias"]
                item["labels"] = [names[key]]
    if desired_native != native:
        raise Error("Compiled graph differs from approved catalog labels or live non-label fields")
    expected_live = {path: payload for path, payload in payloads.items() if path != ".platform"}
    expected_live["graphType.json"] = native
    if not p._native_definition_payloads_equal("graph", desired, expected_live):
        raise Error("Compiled graph non-label parts differ from live graph")
    # The compiler supplies the desired labels; retain the live envelope and all
    # unrelated raw payloads rather than publishing a newly compiled definition.
    for kind in ("nodeTypes", "edgeTypes"):
        for item, wanted in zip(native[kind], desired_native[kind], strict=True):
            item["labels"] = copy.deepcopy(wanted["labels"])
    result = copy.deepcopy(definition)
    for part in result["parts"]:
        if part["path"] == "graphType.json":
            part["payload"] = base64.b64encode(canonical_json(native).encode()).decode()
    label_diff(definition, result)
    return result, mapping


def _local(*, graph_id: str, **inputs: Any) -> dict:
    journal = p._read_json(inputs["journal_path"])
    ontology_id = journal["actions"]["create:ontology"]["item_id"]
    context = o._local(ontology_id=ontology_id, **inputs)
    plan = context["plan"]
    definition = p._read_json(inputs["materialize"] / "native-bound" / "graph.json")
    if o._decode(definition) != o._decode(o._bound_template(
        plan["native_definition_templates"]["graph"], context["lakehouse_id"],
    )):
        raise Error("Original graph differs from approved bound template")
    action = journal["actions"]["create:graph"]
    if (
        graph_id in {ontology_id, context["lakehouse_id"], *journal["baseline_item_ids"]}
        or action.get("item_id") != graph_id or action.get("returned_item_id") != graph_id
        or action.get("kind") != "graph" or action.get("status") != "identity-verified"
        or action.get("ownership") is not None or action.get("http_status") not in (200, 201, 202)
        or action.get("http_status") == 202 and action.get("operation_state") != "Succeeded"
        or action.get("display_name") != plan["names"]["graph"]
        or action.get("request_hash") != canonical_sha256({
            "displayName": plan["names"]["graph"], "description": plan["description"], "definition": definition,
        })
        or journal.get("graph_content_readbacks", {}).get(graph_id, {}).get("status") != "verified"
    ):
        raise Error("Requires the original returned-ID-owned, content-verified independent Graph")
    catalog = context["compilation"].definitions["ontology"]["presentation_catalog"]
    if catalog != readable_catalog_from_domain(context["evidence"]["approved_domain_contract"]):
        raise Error("Presentation catalog differs from sealed approved Domain")
    context["original_graph"] = definition
    context["evidence"].update({
        "original_graph_bytes_hash": o._digest(inputs["materialize"] / "native-bound" / "graph.json"),
        "graph_repair_code_hash": o._digest(Path(__file__)),
        "graph_cli_hash": o._digest(Path(__file__).parents[1] / "cli/graph_presentation_cmd.py"),
    })
    return context


class _GraphRun(o._PresentationRun):
    def __init__(self, journal_path: Path, plan: dict, graph_id: str):
        super().__init__(journal_path, plan, graph_id)
        self.graph_id = graph_id
        self.allowed_queries: set[str] = set()
        self.query_requests = 0

    @property
    def update_url(self) -> str:
        return f"{p.API}/workspaces/{self.plan['workspace_id']}/graphModels/{self.graph_id}/updateDefinition?updateMetadata=false"

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        root = f"{p.API}/workspaces/{self.plan['workspace_id']}"
        query_url = f"{root}/graphModels/{self.graph_id}/executeQuery?beta=true"
        if method == "POST" and url == self.update_url and self.update_allowed and not self.readback_only:
            self.update_allowed = False
        elif method == "POST" and (url == query_url or url.startswith(query_url + "&continuationToken=")):
            if set(kwargs.get("json", {})) != {"query"} or kwargs["json"]["query"] not in self.allowed_queries:
                raise Error("Unplanned graph query forbidden")
            self.query_requests += 1
            if self.query_requests > self.plan["graph_readback_policy"]["max_requests"]:
                raise Error("Graph readback request cap exhausted")
        elif method == "POST" and url == f"{root}/graphModels/{self.graph_id}/getDefinition":
            if kwargs:
                raise Error("Graph definition reads do not accept mutation payloads")
        elif method == "GET" and (
            url == f"{root}/items" or url.startswith(f"{root}/items?")
            or url == f"{root}/graphModels/{self.graph_id}"
            or url == f"{root}/lakehouses/{self.data['actions']['create:lakehouse']['item_id']}"
            or re.fullmatch(re.escape(p.API) + r"/operations/[0-9a-f-]{36}(/result)?", url)
        ):
            pass
        else:
            raise Error("Graph labels-only repair forbids unrelated reads/mutations")
        response = p._Run.request(self, method, url, **kwargs)
        self.receive(response, f"{method}:{url}")
        return response

    def poll(self, headers: dict[str, str], *, definition_result: bool) -> dict:
        # Fabric can return a regional Location. Never send credentials there;
        # validate its operation ID and poll only the canonical API origin.
        if headers.get("x-ms-operation-id") and headers.get("location"):
            operation = str(uuid.UUID(headers["x-ms-operation-id"]))
            parsed = urlsplit(headers["location"])
            if (
                parsed.scheme != "https" or parsed.username or parsed.password or parsed.port
                or not parsed.hostname or parsed.query or parsed.fragment
                or not (parsed.hostname == "api.fabric.microsoft.com" or re.fullmatch(
                    r"wabi-[a-z0-9-]+\.analysis\.windows\.net", parsed.hostname,
                ))
                or parsed.path not in (f"/v1/operations/{operation}", f"/v1/operations/{operation}/result")
            ):
                raise Error("Fabric operation Location/ID mismatch")
            headers = {**headers, "location": f"{p.API}/operations/{operation}"}
        return super().poll(headers, definition_result=definition_result)


def _content(run: _GraphRun, context: dict, definition: dict) -> dict:
    checks = p._graph_readback_checks(
        definition, context["compilation"], run.plan["workspace_id"], context["lakehouse_id"], companion=False,
    )
    policy = run.plan["graph_readback_policy"]
    expected = {
        "nodes": sum(len(c.rows) for c in checks if c.match.startswith("(n:")),
        "edges": sum(len(c.rows) for c in checks if c.match.startswith("(s:")),
    }
    queries = {q for check in checks for q, _ in check.windows(policy["page_size"])}
    if len(queries) + 2 > policy["max_requests"] or sum(expected.values()) > policy["max_total_rows"]:
        raise Error("Full graph readback exceeds approved limits")
    run.allowed_queries = queries | {
        "MATCH (n) RETURN count(*) AS observed_count",
        "MATCH ()-[e]->() RETURN count(*) AS observed_count",
    }
    run.query_requests = 0
    run.data["graph_readback_request_count"] = 0
    run.data["graph_readback_row_count"] = 0
    run.data["graph_count_readbacks"] = {}
    run.data["graph_content_readbacks"] = {}
    try:
        run.graph_counts(run.graph_id, expected)
        run.graph_content(run.graph_id, definition, context["compilation"], context["lakehouse_id"])
    finally:
        if run.event_sink:
            run.event_sink({
                "purpose": "full-scalar-topology-readback",
                "counts": run.data.get("graph_count_readbacks", {}).get(run.graph_id),
                "content": run.data.get("graph_content_readbacks", {}).get(run.graph_id),
            })
    return {
        "expected": expected,
        "counts": copy.deepcopy(run.data["graph_count_readbacks"][run.graph_id]),
        "content": copy.deepcopy(run.data["graph_content_readbacks"][run.graph_id]),
    }


def _snapshot(run: _GraphRun, context: dict, expected: dict) -> dict:
    proof = r._proof(run, "graph", run.graph_id, expected, context["lakehouse_id"])
    definition = run.definitions["graph", run.graph_id]
    return {
        "metadata": proof["metadata"], "lakehouse_metadata": proof["lakehouse_metadata"],
        "definition": copy.deepcopy(definition),
        "workspace_item_ids": sorted(item["id"] for item in run.inventory),
    }


def _seal(state: Path, plan: dict, approval: str, inputs: dict) -> tuple[dict, dict]:
    if (
        plan.get("policy") != POLICY or plan.get("plan_hash") != approval
        or canonical_sha256({k: v for k, v in plan.items() if k != "plan_hash"}) != approval
        or plan.get("inputs") != o._input_paths(inputs)
    ):
        raise Error("Exact reviewed graph label plan/input/hash required")
    backup, replacement = p._read_json(state / "backup.json"), p._read_json(state / "replacement.json")
    if (
        canonical_sha256(backup) != plan["backup_hash"]
        or canonical_sha256(replacement) != plan["replacement_hash"]
        or label_diff(backup["definition"], replacement) != plan["allowed_field_diff"]
        or canonical_sha256(p._read_json(state / "before-content.json")) != plan["before_content_hash"]
        or p._read_json(state / "mapping.json") != {"mapping": plan["mapping"], "policy": POLICY}
    ):
        raise Error("Sealed graph backup/replacement/mapping/content changed")
    return backup, replacement


def _verify(run: _GraphRun, context: dict, backup: dict, replacement: dict, state: Path, plan: dict) -> dict:
    observed = _snapshot(run, context, replacement)
    for key in ("metadata", "lakehouse_metadata", "workspace_item_ids"):
        if observed[key] != backup[key]:
            raise Error("Remote identity/metadata/inventory drift after graph repair")
    if label_diff(replacement, observed["definition"]):
        raise Error("Graph labels differ from approved replacement")
    content = _content(run, context, observed["definition"])
    # Read once more after querying to catch definition/metadata races.
    if _snapshot(run, context, replacement) != observed:
        raise Error("Graph definition drift during scalar/topology verification")
    inputs = {
        key: Path(value) if key not in ("workspace_id", "graph_id") else value
        for key, value in plan["inputs"].items()
    }
    if _local(**inputs)["evidence"] != plan["local_evidence"]:
        raise Error("Local authority/code/journal drift during final verification")
    receipt = {
        "policy": POLICY, "authority": NOTICE, "status": "verified-labels-only",
        "plan_hash": plan["plan_hash"], "graph_id": run.graph_id,
        "workspace_id": run.plan["workspace_id"], "lakehouse_id": context["lakehouse_id"],
        "definition_hash": canonical_sha256(observed["definition"]),
        "snapshot": observed, "content": content, "mapping": plan["mapping"],
        "business_fact_approval": False,
        "original_publication_journal": "immutable; not resumed or authorized",
        "managed_graph": "not touched; no readiness assertion",
    }
    receipt["receipt_hash"] = canonical_sha256(receipt)
    o._exclusive(state / "receipt.json", receipt)
    return {"status": receipt["status"], "plan_hash": plan["plan_hash"], "receipt": str(state / "receipt.json"),
            "graph_id": run.graph_id, "expected": content["expected"]}


def repair_graph_labels(
    *, workspace_id: str, graph_id: str, plan_path: Path, journal_path: Path,
    materialize: Path, l4_run: Path, l3_root: Path, state: Path,
    live: bool = False, approve_plan: str | None = None,
    acknowledge_nontransactional: bool = False, resume: bool = False,
    naming_review: Path | None = None,
) -> dict:
    """Read-only planning by default; one explicit update, no blind retries."""
    inputs = {
        "workspace_id": str(uuid.UUID(workspace_id)), "graph_id": str(uuid.UUID(graph_id)),
        "plan_path": plan_path, "journal_path": journal_path, "materialize": materialize,
        "l4_run": l4_run, "l3_root": l3_root,
    }
    if naming_review is not None:
        inputs["naming_review"] = naming_review
    o._paths_safe(state, inputs)
    if live != bool(approve_plan and acknowledge_nontransactional) or (
        not live and (approve_plan or acknowledge_nontransactional or resume)
    ):
        raise Error("Apply/resume requires --live, exact --approve-plan and --acknowledge-nontransactional")
    if not live:
        if state.exists():
            raise Error("Choose a NEW graph repair state; never overwrite sealed evidence")
        context = _local(**inputs)
        state.mkdir(parents=True, exist_ok=False)
        try:
            run = _GraphRun(journal_path, context["plan"], inputs["graph_id"])
            run.event_sink = lambda evidence: o._event(state, evidence)
            backup = _snapshot(run, context, context["original_graph"])
            o._exclusive(state / "backup.json", backup)
            replacement, mapping = render_labels(
                backup["definition"], context["compilation"], inputs["workspace_id"], context["lakehouse_id"],
                naming_review=context.get("naming_review"),
            )
            diff = label_diff(backup["definition"], replacement)
            if not diff:
                raise Error("No labels need repair")
            content = _content(run, context, backup["definition"])
            if _snapshot(run, context, context["original_graph"]) != backup:
                raise Error("Graph changed during planning")
            if _local(**inputs)["evidence"] != context["evidence"]:
                raise Error("Local authority/code drift during planning")
            plan = {
                "policy": POLICY, "authority": NOTICE, "inputs": o._input_paths(inputs),
                "operation": run.update_url, "method": "POST", "business_fact_approval": False,
                "concurrency": "nontransactional-no-CAS; residual race explicitly acknowledged",
                "local_evidence": context["evidence"],
                "publication_plan_hash": context["plan"]["plan_hash"],
                "backup_hash": canonical_sha256(backup), "replacement_hash": canonical_sha256(replacement),
                "mapping": mapping, "allowed_field_diff": diff, "before_content_hash": canonical_sha256(content),
                "expected": content["expected"],
            }
            plan["plan_hash"] = canonical_sha256(plan)
            o._exclusive(state / "before-content.json", content)
            o._exclusive(state / "replacement.json", replacement)
            o._exclusive(state / "mapping.json", {"mapping": mapping, "policy": POLICY})
            o._exclusive(state / "plan.json", plan)
            return {"status": "planned-read-only", "plan_hash": plan["plan_hash"], "state": str(state),
                    "changed_fields": len(diff), "expected": content["expected"]}
        except Exception as error:
            o._event(state, {"purpose": "failure-retained", "error_type": type(error).__name__, "error": str(error)})
            raise
    if not state.is_dir():
        raise Error("Existing sealed graph label repair state required")
    with (state / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("Graph label repair already active") from None
        try:
            plan = p._read_json(state / "plan.json")
            backup, replacement = _seal(state, plan, approve_plan, inputs)
            context = _local(**inputs)
            if context["evidence"] != plan["local_evidence"]:
                raise Error("Local authority/code/journal drift; review a new plan")
            rendered, mapping = render_labels(
                backup["definition"], context["compilation"], inputs["workspace_id"], context["lakehouse_id"],
                naming_review=context.get("naming_review"),
            )
            if rendered != replacement or mapping != plan["mapping"]:
                raise Error("Fresh sealed catalog mapping differs from approved plan")
            if (state / "receipt.json").exists():
                raise Error("Completed receipt is immutable; no further operation")
            run = _GraphRun(journal_path, context["plan"], inputs["graph_id"])
            run.event_sink = lambda evidence: o._event(state, evidence)
            intent = {
                "policy": POLICY, "plan_hash": approve_plan, "operation": run.update_url,
                "request_hash": canonical_sha256({"definition": replacement}), "authority": NOTICE,
            }
            intent_path = state / "update-intent.json"
            if resume:
                if not intent_path.is_file() or p._read_json(intent_path) != intent:
                    raise Error("Resume requires the exact durable intent; never initiates POST")
                run.readback_only = True
                response_path = state / "update-response.json"
                if response_path.exists():
                    evidence = p._read_json(response_path)
                    if evidence["http_status"] == 202:
                        run.poll(evidence["headers"], definition_result=False)
                return _verify(run, context, backup, replacement, state, plan)
            if intent_path.exists():
                raise Error("Durable intent exists; use read-only --resume, never repeat POST")
            if _snapshot(run, context, context["original_graph"]) != backup:
                raise Error("Fresh remote graph drift; update forbidden")
            _content(run, context, backup["definition"])
            if _snapshot(run, context, context["original_graph"]) != backup:
                raise Error("Graph changed immediately before update")
            if _local(**inputs)["evidence"] != plan["local_evidence"]:
                raise Error("Local authority/code drift immediately before update")
            o._exclusive(intent_path, intent)
            run.update_allowed = True
            response = run.request("POST", run.update_url, json={"definition": replacement})
            evidence = o._response_evidence(response)
            o._exclusive(state / "update-response.json", evidence)
            if response.status_code == 202:
                run.poll(evidence["headers"], definition_result=False)
            elif response.status_code != 200:
                raise Error(f"Graph update HTTP {response.status_code}; retain backup, no retry/rollback")
            return _verify(run, context, backup, replacement, state, plan)
        except Exception as error:
            o._event(state, {
                "purpose": "failure-retained", "error_type": type(error).__name__, "error": str(error),
                "next": "Inspect evidence; if intent exists only --resume may read/poll; no automatic rollback",
            })
            raise
