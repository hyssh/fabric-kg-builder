"""Operator-reviewed recovery; never infer a lost create response or retry it."""

from __future__ import annotations

import copy
import base64
import fcntl
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.deploy import schema2_prototype as p

Error = p.PrototypePublicationError
POLICY = "operator-reconciled-create-and-exact-runtime-repair-v1"


def _semantic(plan: dict[str, Any]) -> dict[str, Any]:
    # No other plan fields, including source authority or limits, are excluded.
    return {key: value for key, value in plan.items() if key not in ("compiler_hash", "plan_hash")}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _valid_plan(plan: dict[str, Any]) -> None:
    if (
        plan.get("plan_version") != p.FORMAT_VERSION
        or plan.get("mode") != "prototype-create-only"
        or plan.get("policy") != "create-only-retain-partial-no-delete-no-overwrite-no-adoption"
        or plan.get("plan_hash") != canonical_sha256({
            key: value for key, value in plan.items() if key != "plan_hash"
        })
        or plan.get("blockers")
    ):
        raise Error("Invalid, changed, or blocked immutable prototype plan")


def recompile_plan(
    plan_path: Path, journal_path: Path, materialize: Path, l4_run: Path, l3_root: Path,
) -> dict[str, Any]:
    plan = p._read_json(plan_path)
    _valid_plan(plan)
    return p.publish_schema2_prototype(
        l4_run=l4_run, l3_root=l3_root, workspace_id=plan["workspace_id"],
        name_prefix=plan["name_prefix"], dry_run=True, approve_live=None,
        plan_path=plan_path, journal_path=journal_path, materialize_dir=materialize,
        approved_limitations=tuple(plan["approved_limitations"]),
        semantic_model=plan["semantic_model"],
        readback_page_size=plan["graph_readback_policy"]["page_size"],
        readback_total_rows=plan["graph_readback_policy"]["max_total_rows"],
        _candidate_only=True,
    )


def _approval(journal: dict[str, Any]) -> dict[str, Any]:
    receipt = journal.get("runtime_repair", {})
    review = receipt.get("review", {})
    if (
        receipt.get("policy") != POLICY or receipt.get("status") != "accepted"
        or not isinstance(receipt.get("actor"), str) or not receipt["actor"].strip()
        or not isinstance(receipt.get("rationale"), str) or not receipt["rationale"].strip()
        or not receipt.get("accepted_at")
        or review.get("authority") != "review-only-not-authorizing"
        or receipt.get("review_hash") != canonical_sha256(review)
    ):
        raise Error("Explicit accepted operator runtime-repair review required")
    return receipt


def validate_runtime_repair(
    original: dict[str, Any], candidate: dict[str, Any], journal: dict[str, Any],
    *, plan_bytes: bytes,
) -> None:
    _valid_plan(original)
    _valid_plan(candidate)
    if _semantic(original) != _semantic(candidate):
        raise Error("Runtime repair rejected: recompiled semantic plan differs")
    receipt = _approval(journal)
    review = receipt["review"]
    if (
        review.get("plan_hash") != original["plan_hash"]
        or review.get("original_plan_bytes_hash") != _digest(plan_bytes)
        or review.get("original_compiler_hash") != original["compiler_hash"]
        or review.get("current_compiler_hash") != candidate["compiler_hash"]
        or candidate["compiler_hash"] != p._compiler_hash()
        or review.get("semantic_comparison_hash") != canonical_sha256(_semantic(candidate))
        or journal.get("plan_hash") != original["plan_hash"]
        or journal.get("run_id") != original["run_id"]
        or journal.get("workspace_id") != original["workspace_id"]
    ):
        raise Error("Runtime-repair receipt/compiler/source/plan binding mismatch")
    action = journal.get("actions", {}).get(f"create:{review.get('kind')}", {})
    if (
        action.get("ownership") != "operator-reconciled"
        or action.get("reconciliation_review_hash") != receipt["review_hash"]
        or action.get("item_id") != review.get("item_id")
        or action.get("original_create_evidence") != review.get("original_create_evidence")
    ):
        raise Error("Runtime-repair receipt has no matching reconciled create")


class _ReadOnlyRun(p._Run):
    def save(self) -> None:
        # getDefinition uses POST but is a read operation, never an item mutation.
        pass

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        if method != "GET" and not (method == "POST" and url.endswith("/getDefinition")):
            raise Error("Reconciliation forbids Fabric mutations")
        return super().request(method, url, **kwargs)


def _identity(run: p._Run, kind: str, item_id: str) -> dict[str, Any]:
    metadata = run.checked(run.request(
        "GET", f"{p.API}/workspaces/{run.plan['workspace_id']}/{p.COLLECTIONS[kind]}/{item_id}",
    ))
    if (
        metadata.get("id") != item_id
        or metadata.get("workspaceId") != run.plan["workspace_id"]
        or metadata.get("type") != p.ITEM_TYPES[kind]
        or metadata.get("displayName") != run.plan["names"][kind]
        or metadata.get("description") != run.plan["description"]
    ):
        raise Error("Reconciliation item ID/workspace/type/full name/run description mismatch")
    return metadata


def _platform_envelope(
    definition: dict[str, Any], plan: dict[str, Any], kind: str,
) -> dict[str, Any] | None:
    parts = [part for part in definition["parts"] if part.get("path") == ".platform"]
    if not parts:
        return None
    if len(parts) != 1 or parts[0].get("payloadType") != "InlineBase64":
        raise Error("Unverifiable or duplicate service .platform envelope")
    try:
        envelope = json.loads(base64.b64decode(parts[0]["payload"], validate=True).decode("utf-8-sig"))
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"$schema", "metadata", "config"}
            or envelope["$schema"]
            != "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"
            or envelope["metadata"] != {
                "type": p.ITEM_TYPES[kind], "displayName": plan["names"][kind],
                "description": plan["description"],
            }
            or not isinstance(envelope["config"], dict)
            or set(envelope["config"]) != {"version", "logicalId"}
            or envelope["config"]["version"] != "2.0"
            or str(uuid.UUID(envelope["config"]["logicalId"])) != envelope["config"]["logicalId"]
        ):
            raise Error("Service .platform metadata/schema/config does not match the exact planned item")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise Error("Invalid service .platform envelope") from error
    return envelope


def _proof(
    run: p._Run, kind: str, item_id: str, definition: dict[str, Any], lakehouse_id: str,
) -> dict[str, Any]:
    baseline = run.data.get("baseline_item_ids")
    if not isinstance(baseline, list) or item_id in baseline or lakehouse_id in baseline:
        raise Error("Reconciliation requires both IDs absent from the recorded baseline")
    lakehouse = run.data.get("actions", {}).get("create:lakehouse", {})
    if (
        lakehouse.get("item_id") != lakehouse_id
        or lakehouse.get("returned_item_id") != lakehouse_id
        or lakehouse.get("status") != "identity-verified"
        or lakehouse.get("http_status") not in (200, 201, 202)
        or lakehouse.get("http_status") == 202 and lakehouse.get("operation_state") != "Succeeded"
        or lakehouse.get("request_hash") != canonical_sha256({
            "displayName": run.plan["names"]["lakehouse"],
            "description": run.plan["description"],
            "creationPayload": {"enableSchemas": True},
        })
    ):
        raise Error("Reconciliation requires the original returned-ID-owned Lakehouse")
    lakehouse_metadata = _identity(run, "lakehouse", lakehouse_id)
    if p.resolve_lakehouse_schema(lakehouse_metadata) != "dbo":
        raise Error("Owned Lakehouse schema changed")
    for table_id, table_proof in run.plan["tables"].items():
        delta = run.data["actions"].get(f"delta:{table_id}", {})
        path = (
            f"abfss://{run.plan['workspace_id']}@onelake.dfs.fabric.microsoft.com"
            f"/{lakehouse_id}/{p.onelake_tables_path('dbo', table_id)}"
        )
        if (
            delta.get("status") != "verified" or delta.get("delta_version") != 0
            or delta.get("table_proof") != table_proof or delta.get("readback") != table_proof
            or delta.get("path") != path
        ):
            raise Error("Reconciliation requires prior verified Delta proofs in the same owned Lakehouse")
    metadata = _identity(run, kind, item_id)
    matches = [
        item for item in run.items()
        if item.get("displayName", "").casefold() == run.plan["names"][kind].casefold()
    ]
    if len(matches) != 1 or matches[0].get("id") != item_id:
        raise Error("Reconciliation name collision or inventory race")
    actual_definition = run.definition(kind, item_id)
    platform = _platform_envelope(actual_definition, run.plan, kind)
    expected_platform = _platform_envelope(definition, run.plan, kind)
    if expected_platform is not None and platform != expected_platform:
        raise Error("Planned .platform envelope missing or changed")
    actual = p._definition_payloads(actual_definition)
    expected = p._definition_payloads(definition)
    if not expected or actual != expected:
        raise Error("Reconciliation full native bound definition mismatch")
    if _identity(run, kind, item_id) != metadata:
        raise Error("Reconciliation metadata changed during readback")
    return {
        "metadata": metadata, "lakehouse_metadata": lakehouse_metadata,
        "definition_hash": canonical_sha256(actual), "lakehouse_id": lakehouse_id,
        "service_platform_hash": canonical_sha256(platform) if platform is not None else None,
    }


def _artifacts(
    plan: dict[str, Any], materialize: Path, l4_run: Path, l3_root: Path,
    lakehouse_id: str, kind: str,
) -> dict[str, Any]:
    import pyarrow.parquet as pq

    compilation = p._compile(l4_run, l3_root, plan["workspace_id"], plan["name_prefix"])
    if (
        compilation.provenance != plan["provenance"]
        or {name: p._table_proof(table) for name, table in sorted(compilation.tables.items())}
        != plan["tables"]
    ):
        raise Error("Reconciliation sealed source changed")
    if {path.stem for path in (materialize / "tables").glob("*.parquet")} != set(plan["tables"]):
        raise Error("Reconciliation materialized table set changed")
    for name, proof in plan["tables"].items():
        if p._table_proof(pq.read_table(materialize / "tables" / f"{name}.parquet")) != proof:
            raise Error("Reconciliation materialized table values/schema changed")
    for name, definition in compilation.definitions.items():
        if p._read_json(materialize / "definitions" / f"{name}.json") != definition:
            raise Error("Reconciliation materialized semantic definition changed")
    for name, definition in plan["native_definition_templates"].items():
        if p._read_json(materialize / "native-templates" / f"{name}.json") != definition:
            raise Error("Reconciliation native template changed")
    native, _ = p._native_definitions(
        compilation, workspace_id=plan["workspace_id"], lakehouse_id=lakehouse_id,
        names=plan["names"], description=plan["description"], root=materialize,
        semantic_model=plan["semantic_model"],
    )
    definition = p._read_json(materialize / "native-bound" / f"{kind}.json")
    if definition != native[kind]:
        raise Error("Reconciliation materialized native bound definition changed")
    return definition


def validate_reconciled_create(
    run: p._Run, kind: str, definition: dict[str, Any] | None,
) -> str:
    receipt = _approval(run.data)
    review = receipt["review"]
    action = run.data["actions"][f"create:{kind}"]
    original = review.get("original_create_evidence", {})
    if (
        review.get("kind") != kind or review.get("plan_hash") != run.plan["plan_hash"]
        or review.get("original_compiler_hash") != run.plan["compiler_hash"]
        or review.get("current_compiler_hash") != p._compiler_hash()
        or review.get("semantic_comparison_hash") != canonical_sha256(_semantic(run.plan))
        or action.get("reconciliation_review_hash") != receipt["review_hash"]
        or action.get("original_create_evidence") != original
        or any(key not in action or action[key] != value for key, value in original.items())
        or any(key in action and key not in original for key in (
            "http_status", "returned_item_id", "operation_id", "location",
        ))
        or action.get("item_id") != review.get("item_id")
        or definition is None
    ):
        raise Error("Reconciled create receipt/original intent mismatch")
    if _proof(run, kind, action["item_id"], definition, review["proof"]["lakehouse_id"]) != review["proof"]:
        raise Error("Reconciled create fresh readback changed")
    return action["item_id"]


def reconcile_prototype_create(
    *, plan_path: Path, journal_path: Path, materialize: Path, l4_run: Path, l3_root: Path,
    kind: str, item_id: str, review_path: Path, accept_review: str | None = None,
    actor: str | None = None, rationale: str | None = None,
) -> dict[str, Any]:
    if kind not in ("ontology", "graph", "semantic_model"):
        raise Error("Only a native-definition create can be reconciled")
    item_id = str(uuid.UUID(item_id))
    if len({path.resolve() for path in (plan_path, journal_path, review_path)}) != 3:
        raise Error("Plan, journal and review must be distinct files")
    if review_path.resolve().is_relative_to(materialize.resolve()):
        raise Error("Review must be outside immutable materialized artifacts")
    if bool(actor or rationale) != bool(accept_review) or (
        accept_review and (not actor or not actor.strip() or not rationale or not rationale.strip())
    ):
        raise Error("Acceptance requires exact review hash, actor and rationale together")
    with journal_path.with_suffix(journal_path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("This prototype journal is already active") from None
        plan_bytes, journal_bytes = plan_path.read_bytes(), journal_path.read_bytes()
        plan = p._read_json(plan_path)
        candidate = recompile_plan(plan_path, journal_path, materialize, l4_run, l3_root)
        if _semantic(plan) != _semantic(candidate):
            raise Error("Runtime repair rejected: recompiled semantic plan differs")
        run = _ReadOnlyRun(journal_path, plan)
        if run.data.get("workspace_id") != plan["workspace_id"]:
            raise Error("Foreign journal workspace")
        action = run.data["actions"].get(f"create:{kind}", {})
        if (
            action.get("status") not in ("intent", "response_received")
            or action.get("item_id") or action.get("ownership") or action.get("returned_item_id")
            or action.get("http_status") not in (None, 200, 201, 202)
            or action.get("operation_state") in ("Failed", "Cancelled")
            or action.get("kind") != kind
        ):
            raise Error("Reconciliation requires an unresolved original create intent without an item ID")
        original = copy.deepcopy(action)
        lakehouse_id = str(uuid.UUID(run.data["actions"]["create:lakehouse"]["item_id"]))
        definition = _artifacts(plan, materialize, l4_run, l3_root, lakehouse_id, kind)
        request = {
            "displayName": plan["names"][kind], "description": plan["description"],
            "definition": definition,
        }
        if action.get("request_hash") != canonical_sha256(request):
            raise Error("Reconciliation original create request hash mismatch")
        proof = _proof(run, kind, item_id, definition, lakehouse_id)
        review = {
            "policy": POLICY, "authority": "review-only-not-authorizing",
            "plan_hash": plan["plan_hash"], "original_plan_bytes_hash": _digest(plan_bytes),
            "journal_snapshot_hash": _digest(journal_bytes),
            "original_compiler_hash": plan["compiler_hash"],
            "current_compiler_hash": candidate["compiler_hash"],
            "semantic_comparison_hash": canonical_sha256(_semantic(candidate)),
            "kind": kind, "item_id": item_id, "original_create_evidence": original,
            "proof": proof,
            "ownership_basis": "explicit-operator-reconciliation-not-a-returned-create-ID",
            "allowed_effect": "journal-only; no Fabric create/update/delete",
        }
        review_hash = canonical_sha256(review)
        output = {"review_hash": review_hash, "review": review}
        if not accept_review:
            p._atomic_json(review_path, output, create=True)
            return output
        if accept_review != review_hash or p._read_json(review_path) != output:
            raise Error("Accepted review hash/readbacks/journal changed; generate a new review")
        if _proof(run, kind, item_id, definition, lakehouse_id) != proof:
            raise Error("Reconciliation readback race; no acceptance recorded")
        if (
            plan_path.read_bytes() != plan_bytes or journal_path.read_bytes() != journal_bytes
            or p._compiler_hash() != candidate["compiler_hash"]
            or recompile_plan(plan_path, journal_path, materialize, l4_run, l3_root) != candidate
            or _artifacts(plan, materialize, l4_run, l3_root, lakehouse_id, kind) != definition
        ):
            raise Error("Reconciliation local source/artifact/journal race; no acceptance recorded")
        # Persist only the approved reconciliation, not the in-memory read-operation log.
        journal = p._read_json(journal_path)
        action = journal["actions"][f"create:{kind}"]
        action.update({
            "item_id": item_id, "metadata": proof["metadata"], "ownership": "operator-reconciled",
            "original_create_evidence": original, "reconciliation_review_hash": review_hash,
        })
        journal["runtime_repair"] = {
            "policy": POLICY, "status": "accepted", "review_hash": review_hash, "review": review,
            "actor": actor.strip(), "rationale": rationale.strip(),
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }
        validate_runtime_repair(plan, candidate, journal, plan_bytes=plan_bytes)
        p._atomic_json(journal_path, journal)
        return {"status": "accepted-journal-only", "review_hash": review_hash, "item_id": item_id}
