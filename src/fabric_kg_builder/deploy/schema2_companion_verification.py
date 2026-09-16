"""Separately approved, read-only proof; no publication or journal authorization."""

from __future__ import annotations

import copy
import base64
import fcntl
import json
import uuid
from pathlib import Path
from typing import Any

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.deploy import schema2_prototype as p
from fabric_kg_builder.deploy import schema2_prototype_reconcile as r

POLICY = "read-only-native-companion-verification-v1"


def _runtime_hash() -> str:
    return canonical_sha256({
        "publisher": p._compiler_hash(),
        "verifier": r._digest(Path(__file__).read_bytes()),
        "cli": r._digest((Path(__file__).parents[1] / "cli/prototype_reconcile_cmd.py").read_bytes()),
    })


def _prepare(
    plan_path: Path, journal_path: Path, materialize: Path, l4_run: Path, l3_root: Path,
    companion_id: str, companion_definition_path: Path,
) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    original, journal = p._read_json(plan_path), p._read_json(journal_path)
    receipt = r.validate_prior_returned_repair(original, journal, plan_bytes=plan_path.read_bytes())
    r.validate_returned_artifacts(journal, materialize)
    candidate = r.recompile_plan(plan_path, journal_path, materialize, l4_run, l3_root)
    if canonical_sha256(r._semantic(candidate)) != canonical_sha256(r._semantic(original)):
        raise p.PrototypePublicationError("Read-only verification changed the original semantic plan")
    actions = journal["actions"]
    ids = {kind: actions[f"create:{kind}"]["item_id"] for kind in ("lakehouse", "ontology", "graph")}
    if (
        original["semantic_model"] or journal.get("status") != "partial"
        or journal.get("structural_publication")
        != "definitions-delta-values-and-independent-graph-scalars-topology-verified"
        or journal.get("graph_content_readbacks", {}).get(ids["graph"], {}).get("status") != "verified"
        or journal.get("item_ids") != ids
        or ids["graph"] != receipt["review"]["item_id"]
        or actions["create:graph"].get("definition_readback_hash") != receipt["review"]["proof"]["definition_hash"]
        or companion_id in {*ids.values(), *journal["baseline_item_ids"]}
    ):
        raise p.PrototypePublicationError("Requires the retained, independently verified partial prototype")
    for kind, item_id in ids.items():
        action = actions[f"create:{kind}"]
        if (
            action.get("returned_item_id") != item_id or action.get("status") != "identity-verified"
            or action.get("ownership") is not None
            or action.get("kind") != kind or type(action.get("http_status")) is not int
            or action["http_status"] not in (200, 201, 202)
            or action["http_status"] == 202 and action.get("operation_state") != "Succeeded"
            or any(action.get("metadata", {}).get(key) != value for key, value in {
                "id": item_id, "type": p.ITEM_TYPES[kind], "workspaceId": original["workspace_id"],
                "displayName": original["names"][kind], "description": original["description"],
            }.items())
        ):
            raise p.PrototypePublicationError("Read-only verification requires unchanged returned-ID ownership")
    definitions = {
        kind: r._artifacts(original, materialize, l4_run, l3_root, ids["lakehouse"], kind)
        for kind in ("graph", "ontology")
    }
    for kind, definition in definitions.items():
        if actions[f"create:{kind}"]["request_hash"] != canonical_sha256({
            "displayName": original["names"][kind], "description": original["description"],
            "definition": definition,
        }):
            raise p.PrototypePublicationError("Original native create request changed")
    snapshot = p._read_json(companion_definition_path)
    if set(snapshot) == {"definition"}:
        snapshot = snapshot["definition"]
    p._validate_companion_parts(snapshot)
    compilation = p._compile_from_plan(l4_run, l3_root, original)
    checks = {}
    for kind, definition in (("graph", definitions["graph"]), ("companion", snapshot)):
        checks[kind] = p._graph_readback_checks(
            definition, compilation, original["workspace_id"], ids["lakehouse"],
            companion=kind == "companion", native_ontology=definitions["ontology"],
        )
    query_windows = {
        kind: [
            {
                "query": query, "expected_rows": len(rows),
                "expected_hash": p._graph_row_fingerprint(rows, check.fields, from_wire=False),
            }
            for check in values for query, rows in check.windows(original["graph_readback_policy"]["page_size"])
        ]
        for kind, values in checks.items()
    }
    expected = {
        kind: {
            "nodes": sum(len(check.rows) for check in values if check.match.startswith("(n:")),
            "edges": sum(len(check.rows) for check in values if check.match.startswith("(s:")),
        } for kind, values in checks.items()
    }
    limits = original["graph_readback_policy"]
    if (
        sum(sum(value.values()) for value in expected.values()) > limits["max_total_rows"]
        or sum(len(value) for value in query_windows.values()) + 4 > limits["max_requests"]
    ):
        raise p.PrototypePublicationError("Read-only verification exceeds original readback limits")
    verification = {
        "policy": POLICY, "authority": "review-only-not-authorizing",
        "allowed_effect": "Fabric reads and a separate proof file only; no journal/item/Delta mutations",
        "original_plan_hash": original["plan_hash"],
        "original_plan_bytes_hash": r._digest(plan_path.read_bytes()),
        "journal_bytes_hash": r._digest(journal_path.read_bytes()),
        "prior_runtime_repair": copy.deepcopy(receipt),
        "runtime_hash": _runtime_hash(), "current_compiler_hash": candidate["compiler_hash"],
        "semantic_comparison_hash": canonical_sha256(r._semantic(candidate)),
        "artifact_digests": r._artifact_digests(materialize),
        "companion_definition_bytes_hash": r._digest(companion_definition_path.read_bytes()),
        "companion_definition_hash": canonical_sha256(snapshot),
        "workspace_id": original["workspace_id"], "item_ids": {**ids, "companion": companion_id},
        "graph_readback_policy": limits, "expected": expected, "query_windows": query_windows,
    }
    verification["verification_plan_hash"] = canonical_sha256(verification)
    return verification, compilation, {**definitions, "companion": snapshot}


class _VerificationRun(r._ReadOnlyRun):
    def __init__(self, journal_path: Path, original: dict[str, Any], verification: dict[str, Any]) -> None:
        # The publisher constructor can create a missing journal; this path must never do so.
        data = journal_path.read_bytes()
        if r._digest(data) != verification["journal_bytes_hash"]:
            raise p.PrototypePublicationError("Source journal changed before read-only verification")
        self.path, self.plan, self.data = journal_path, original, json.loads(data)
        from azure.identity import AzureCliCredential

        self.credential = AzureCliCredential()
        self.verification = verification
        self.responses: list[dict[str, Any]] = []
        self.query_requests = 0
        self.data["graph_readback_request_count"] = 0
        self.data["graph_readback_row_count"] = 0
        self.data["graph_count_readbacks"] = {}
        self.data["graph_content_readbacks"] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        if method == "POST" and "/executeQuery" in url:
            queries = {
                entry["query"] for entries in self.verification["query_windows"].values() for entry in entries
            } | {
                "MATCH (n) RETURN count(*) AS observed_count",
                "MATCH ()-[e]->() RETURN count(*) AS observed_count",
            }
            prefixes = [
                f"{p.API}/workspaces/{self.plan['workspace_id']}/graphModels/"
                f"{self.verification['item_ids'][kind]}/executeQuery?beta=true"
                for kind in ("graph", "companion")
            ]
            if (
                not any(url == prefix or url.startswith(prefix + "&continuationToken=") for prefix in prefixes)
                or set(kwargs.get("json", {})) != {"query"} or kwargs["json"]["query"] not in queries
            ):
                raise p.PrototypePublicationError("Only planned read-only Graph queries are permitted")
            self.query_requests += 1
            if self.query_requests > self.plan["graph_readback_policy"]["max_requests"]:
                raise p.PrototypePublicationError("Original Graph readback request cap exhausted")
            response = p._Run.request(self, method, url, **kwargs)
        else:
            response = super().request(method, url, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = None
        raw = response.content or b""
        self.responses.append({
            "method": method, "url": url, "request": kwargs.get("json"),
            "status_code": response.status_code, "body": copy.deepcopy(body),
            "body_hash": canonical_sha256(body),
            "headers": dict(response.headers),
            "raw_body_base64": base64.b64encode(raw).decode("ascii"),
            "raw_body_hash": r._digest(raw),
        })
        return response


def _companion_proof(
    run: _VerificationRun, snapshot: dict[str, Any],
) -> dict[str, Any]:
    ids = run.verification["item_ids"]
    url = f"{p.API}/workspaces/{run.plan['workspace_id']}/graphModels/{ids['companion']}"
    metadata = run.checked(run.request("GET", url))
    if (
        metadata.get("id") != ids["companion"] or metadata.get("workspaceId") != run.plan["workspace_id"]
        or metadata.get("type") != "GraphModel"
        or ids["ontology"].replace("-", "") not in metadata.get("displayName", "")
    ):
        raise p.PrototypePublicationError("Companion metadata/workspace/Ontology linkage mismatch")
    candidates = [
        item for item in run.items()
        if item.get("type") == "GraphModel"
        and ids["ontology"].replace("-", "") in item.get("displayName", "")
        and item.get("id") not in run.data["baseline_item_ids"]
    ]
    if len(candidates) != 1 or candidates[0].get("id") != ids["companion"]:
        raise p.PrototypePublicationError("Companion inventory is ambiguous or changed")
    definition = run.definition("graph", ids["companion"])
    p._validate_companion_parts(definition, metadata)
    if canonical_sha256(definition) != canonical_sha256(snapshot):
        raise p.PrototypePublicationError("Companion native definition differs from reviewed snapshot")
    return {"metadata": metadata, "native_definition": definition, "definition_hash": canonical_sha256(definition)}


def verify_prototype_companion(
    *, plan_path: Path, journal_path: Path, materialize: Path, l4_run: Path, l3_root: Path,
    companion_id: str, companion_definition_path: Path, verification_plan_path: Path,
    proof_path: Path, approve_readback: str | None = None,
) -> dict[str, Any]:
    companion_id = str(uuid.UUID(companion_id))
    paths = (plan_path, journal_path, companion_definition_path, verification_plan_path, proof_path)
    if len({path.resolve() for path in paths}) != len(paths) or any(
        output.resolve().is_relative_to(root.resolve())
        for output in (verification_plan_path, proof_path) for root in (materialize, l4_run, l3_root)
    ):
        raise p.PrototypePublicationError("Verification outputs must be distinct and outside sealed inputs")
    with journal_path.with_suffix(journal_path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise p.PrototypePublicationError("This prototype journal is already active") from None
        verification, compilation, definitions = _prepare(
            plan_path, journal_path, materialize, l4_run, l3_root, companion_id, companion_definition_path,
        )
        if not approve_readback:
            p._atomic_json(verification_plan_path, verification, create=True)
            return verification
        if (
            approve_readback != verification["verification_plan_hash"]
            or p._read_json(verification_plan_path) != verification
            or proof_path.exists()
        ):
            raise p.PrototypePublicationError("Readback approval/inputs changed or proof already exists")
        original = p._read_json(plan_path)
        run = _VerificationRun(journal_path, original, verification)
        ids = verification["item_ids"]
        result: dict[str, Any] = {
            "policy": POLICY, "verification_plan_hash": approve_readback,
            "original_plan_hash": original["plan_hash"], "journal_bytes_hash": verification["journal_bytes_hash"],
            "authority": "read-only-evidence-not-publication-or-ownership-authorization",
            "historical_journal_errors": {
                key: copy.deepcopy(run.data.get(key))
                for key in ("status", "last_error", "companion_readback_errors")
            },
        }
        try:
            graph = r._returned_proof(run, "graph", ids["graph"], definitions["graph"], ids["lakehouse"])
            if graph != verification["prior_runtime_repair"]["review"]["proof"]:
                raise p.PrototypePublicationError("Prior returned-ID live native/create proof changed")
            ontology = r._proof(
                run, "ontology", ids["ontology"], definitions["ontology"], ids["lakehouse"], retain_definition=True,
            )
            companion = _companion_proof(run, definitions["companion"])
            for kind, definition in (("graph", graph["native_definition"]), ("companion", companion["native_definition"])):
                run.graph_counts(ids[kind], verification["expected"][kind])
                run.graph_content(
                    ids[kind], definition, compilation, ids["lakehouse"], companion=kind == "companion",
                    native_ontology=ontology["native_definition"],
                )
            if (
                _companion_proof(run, definitions["companion"]) != companion
                or r._returned_proof(run, "graph", ids["graph"], definitions["graph"], ids["lakehouse"]) != graph
                or r._proof(
                    run, "ontology", ids["ontology"], definitions["ontology"], ids["lakehouse"], retain_definition=True,
                ) != ontology
                or _prepare(
                    plan_path, journal_path, materialize, l4_run, l3_root, companion_id, companion_definition_path,
                )[0] != verification
            ):
                raise p.PrototypePublicationError("Live definition/metadata or sealed input changed during verification")
            result.update({
                "status": "independent-and-companion-scalars-topology-verified",
                "item_proofs": {"graph": graph, "ontology": ontology, "companion": companion},
                "counts": {ids[kind]: run.data["graph_count_readbacks"][ids[kind]] for kind in ("graph", "companion")},
                "content": {ids[kind]: run.data["graph_content_readbacks"][ids[kind]] for kind in ("graph", "companion")},
                "expected": verification["expected"],
            })
        except Exception as error:
            result.update({"status": "failed-read-only", "error": {"type": type(error).__name__, "message": str(error)}})
            raise
        finally:
            result["counts"] = run.data["graph_count_readbacks"]
            result["content"] = run.data["graph_content_readbacks"]
            result["responses"] = run.responses
            result["proof_hash"] = canonical_sha256(result)
            p._atomic_json(proof_path, result, create=True)
        return result
