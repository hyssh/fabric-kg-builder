"""Review-first, same-item ontology presentation repair; no create or delete."""

from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.deploy import schema2_prototype as p
from fabric_kg_builder.deploy import schema2_prototype_reconcile as r

Error = p.PrototypePublicationError
POLICY = "ontology-presentation-only-same-item-nontransactional-v1"
READBACK_POLICY = "pairwise-relationship-empty-customAttributes-only-v1"
MAX_RESPONSE_BYTES = 65536
MAX_POLLS = 120


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decode(definition: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(definition, dict) or set(definition) != {"parts"}:
        raise Error("Expected a complete native definition containing only parts")
    if not isinstance(definition["parts"], list) or not definition["parts"]:
        raise Error("Empty or invalid native definition")
    paths: set[str] = set()
    for part in definition["parts"]:
        if (
            not isinstance(part, dict)
            or set(part) != {"path", "payload", "payloadType"}
            or not isinstance(part["path"], str)
            or part["path"] in paths
            or part["payloadType"] != "InlineBase64"
        ):
            raise Error("Unverifiable or duplicate native definition part")
        paths.add(part["path"])
    try:
        values = p._definition_payloads(definition)
        for part in definition["parts"]:
            if part["path"] == ".platform":
                values[".platform"] = json.loads(
                    base64.b64decode(part["payload"], validate=True).decode("utf-8-sig")
                )
        return values
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise Error("Invalid native definition payload") from error


def _content_hash(definition: dict[str, Any]) -> str:
    return canonical_sha256(_decode(definition))


def _readback_equivalence(planned: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    expected, actual = _decode(planned), _decode(observed)
    normalized = copy.deepcopy(actual)
    defaults = []
    for path, payload in expected.items():
        if not re.fullmatch(r"RelationshipTypes/[^/]+/definition\.json", path):
            continue
        if not isinstance(payload, dict) or not isinstance(normalized.get(path), dict):
            continue
        old_enrichment = payload.get("semanticEnrichment")
        new_enrichment = normalized[path].get("semanticEnrichment")
        if (
            isinstance(old_enrichment, dict) and isinstance(new_enrichment, dict)
            and isinstance(old_enrichment.get("description"), str)
            and "customAttributes" not in old_enrichment
            and "customAttributes" in new_enrichment
            and type(new_enrichment["customAttributes"]) is dict
            and new_enrichment["customAttributes"] == {}
        ):
            del new_enrichment["customAttributes"]
            defaults.append({
                "part": path, "pointer": "/semanticEnrichment/customAttributes",
                "planned": "absent", "observed": {},
            })
    if canonical_sha256(normalized) != canonical_sha256(expected):
        raise Error("Readback definition mismatch; not successful; backup retained; no automatic rollback")
    return {
        "policy": READBACK_POLICY,
        "approved_planned_after_hash": _content_hash(planned),
        "raw_observed_after_hash": _content_hash(observed),
        "observed_definition_envelope_hash": canonical_sha256(observed),
        "normalized_comparison_hash": canonical_sha256(normalized),
        "service_default_paths": defaults,
    }


def _diff(before: Any, after: Any, pointer: str = "") -> list[dict[str, Any]]:
    if type(before) is type(after) and isinstance(before, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            escaped = key.replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                present = after[key] if key in after else before[key]
                if isinstance(present, dict) and present:
                    result.extend(_diff(
                        before.get(key, {}), after.get(key, {}), f"{pointer}/{escaped}",
                    ))
                    continue
                result.append({
                    "pointer": f"{pointer}/{escaped}",
                    "before": before.get(key), "after": after.get(key),
                    "before_present": key in before, "after_present": key in after,
                })
                continue
            result.extend(_diff(before.get(key), after.get(key), f"{pointer}/{escaped}"))
        return result
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [
            change for index, (old, new) in enumerate(zip(before, after))
            for change in _diff(old, new, f"{pointer}/{index}")
        ]
    return [] if type(before) is type(after) and before == after else [
        {"pointer": pointer, "before": before, "after": after},
    ]


def presentation_diff(
    before: dict[str, Any], after: dict[str, Any],
) -> list[dict[str, Any]]:
    """Independently enforce the narrow field allowlist, not renderer assertions."""
    old, new = _decode(before), _decode(after)
    if set(old) != set(new):
        raise Error("Presentation repair cannot change native part paths or counts")
    raw_before = {part["path"]: part for part in before["parts"]}
    raw_after = {part["path"]: part for part in after["parts"]}
    changes = []
    for path in sorted(old):
        entity = re.fullmatch(r"EntityTypes/[^/]+/definition\.json", path)
        relationship = re.fullmatch(r"RelationshipTypes/[^/]+/definition\.json", path)
        if not entity and not relationship:
            if raw_before[path] != raw_after[path]:
                raise Error(f"Protected definition/binding part bytes changed: {path}")
            continue
        for change in _diff(old[path], new[path]):
            pointer = change["pointer"]
            allowed = pointer in ("/name", "/semanticEnrichment/description")
            if entity:
                allowed |= pointer == "/displayNamePropertyId" or bool(
                    re.fullmatch(r"/properties/\d+/(name|semanticEnrichment/description)", pointer)
                )
            if not allowed or not isinstance(change["after"], str) or not change["after"].strip():
                raise Error(f"Protected non-presentation field changed: {path}{pointer}")
            if pointer.endswith("/name") and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", change["after"]):
                raise Error(f"Native ontology name is not a valid readable identifier: {path}{pointer}")
            changes.append({"part": path, **change})
        if entity:
            item = new[path]
            display = item.get("displayNamePropertyId")
            props = {prop["id"]: prop for prop in item["properties"]}
            if display not in props or props[display].get("valueType") != "String":
                raise Error("Display property must be an existing String property")
            if display != old[path].get("displayNamePropertyId"):
                label_bindings = [
                    binding
                    for binding_path, payload in old.items()
                    if binding_path.startswith(path.rsplit("/", 1)[0] + "/DataBindings/")
                    for binding in payload["dataBindingConfiguration"]["propertyBindings"]
                    if binding.get("targetPropertyId") == display
                    and binding.get("sourceColumnName") in ("label", "__label")
                ]
                if not label_bindings:
                    raise Error("Display property must reference the existing bound instance label")
    return changes


def _invariants(definition: dict[str, Any]) -> dict[str, Any]:
    payloads = _decode(definition)
    entities = {
        path: {key: item.get(key) for key in ("id", "entityIdParts", "baseEntityTypeId")}
        | {"property_ids": [prop["id"] for prop in item["properties"]]}
        for path, item in payloads.items()
        if re.fullmatch(r"EntityTypes/[^/]+/definition\.json", path)
    }
    relationships = {
        path: {key: item.get(key) for key in ("id", "source", "target")}
        for path, item in payloads.items()
        if re.fullmatch(r"RelationshipTypes/[^/]+/definition\.json", path)
    }
    bindings = {
        part["path"]: {
            "payload_bytes_hash": hashlib.sha256(base64.b64decode(part["payload"], validate=True)).hexdigest(),
            "json_hash": canonical_sha256(payloads[part["path"]]),
        }
        for part in definition["parts"]
        if "/DataBindings/" in part["path"] or "/Contextualizations/" in part["path"]
    }
    return {
        "part_paths": sorted(payloads), "part_count": len(payloads),
        "entity_type_count": len(entities), "relationship_type_count": len(relationships),
        "entities": entities, "relationships": relationships, "bindings": bindings,
    }


def _bound_template(template: dict[str, Any], lakehouse_id: str) -> dict[str, Any]:
    def bind(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: lakehouse_id if key == "itemId" and item == p.LAKEHOUSE_REFERENCE else bind(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [bind(item) for item in value]
        return value

    from fabric_kg_builder.deploy.fabric_ontology_definition import _part

    return {"parts": [_part(path, bind(value)) for path, value in _decode(template).items()]}


def _ownership(
    plan: dict[str, Any], journal: dict[str, Any], plan_path: Path,
    ontology_id: str, original: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        journal.get("plan_hash") != plan["plan_hash"]
        or journal.get("run_id") != plan["run_id"]
        or journal.get("workspace_id") != plan["workspace_id"]
        or journal.get("journal_version") != p.FORMAT_VERSION
    ):
        raise Error("Journal does not own this exact approved plan/workspace")
    action = journal.get("actions", {}).get("create:ontology", {})
    request_hash = canonical_sha256({
        "displayName": plan["names"]["ontology"],
        "description": plan["description"], "definition": original,
    })
    if (
        action.get("item_id") != ontology_id or action.get("request_hash") != request_hash
        or action.get("kind") != "ontology"
        or action.get("display_name") != plan["names"]["ontology"]
    ):
        raise Error("Original ontology identity/create request hash mismatch")
    if action.get("ownership") == "operator-reconciled":
        receipt = r._approval(journal)
        review = receipt["review"]
        evidence = review.get("original_create_evidence", {})
        if (
            review.get("kind") != "ontology"
            or review.get("item_id") != ontology_id
            or review.get("plan_hash") != plan["plan_hash"]
            or review.get("original_plan_bytes_hash") != _digest(plan_path)
            or review.get("original_compiler_hash") != plan["compiler_hash"]
            or review.get("semantic_comparison_hash") != canonical_sha256(r._semantic(plan))
            or action.get("reconciliation_review_hash") != receipt["review_hash"]
            or action.get("original_create_evidence") != evidence
            or not evidence
            or evidence.get("status") not in ("intent", "response_received")
            or evidence.get("http_status") not in (None, 200, 201, 202)
            or evidence.get("operation_state") in ("Failed", "Cancelled")
            or evidence.get("item_id") or evidence.get("returned_item_id")
            or any(action.get(key) != value for key, value in evidence.items())
            or any(key in action and key not in evidence for key in (
                "http_status", "returned_item_id", "operation_id", "location",
            ))
            or review.get("proof", {}).get("definition_hash")
            != canonical_sha256(p._definition_payloads(original))
        ):
            raise Error("Historical reconciliation proof/original intent mismatch")
        return receipt
    if (
        action.get("returned_item_id") != ontology_id
        or action.get("status") != "identity-verified"
        or action.get("http_status") not in (200, 201, 202)
        or action.get("http_status") == 202 and action.get("operation_state") != "Succeeded"
    ):
        raise Error("Ontology needs exact returned-ID ownership or accepted reconciliation")
    return None


def _render(definition: dict[str, Any], compilation: Any, source: Any) -> tuple[dict, Any]:
    import pyarrow.parquet as pq
    from fabric_kg_builder.deploy.ontology_names import (
        readable_catalog_from_domain, repair_ontology_presentation,
    )
    from fabric_kg_builder.serving.structured_publication import _publication_authority

    domain, _ = _publication_authority({
        "semantic_publication_authority": pq.read_table(source.resolve("semantic_publication_authority")),
    })
    rendered = repair_ontology_presentation(
        definition["parts"], l5a_ontology=compilation.definitions["ontology"],
        catalog=readable_catalog_from_domain(domain), preserve_live_metadata=True,
    )
    return {"parts": list(rendered.parts)}, list(rendered.mapping_report)


def _code_hashes() -> dict[str, str]:
    from fabric_kg_builder.deploy import ontology_names

    return {
        "compiler_hash": p._compiler_hash(),
        "naming_policy_hash": _digest(Path(ontology_names.__file__)),
        "repair_code_hash": _digest(Path(__file__)),
    }


def _local(
    *, plan_path: Path, journal_path: Path, materialize: Path, l4_run: Path,
    l3_root: Path, workspace_id: str, ontology_id: str,
) -> dict[str, Any]:
    import pyarrow.parquet as pq
    from fabric_kg_builder.deploy.ontology_names import readable_catalog_from_domain
    from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
    from fabric_kg_builder.serving.structured_publication import _publication_authority

    plan = p._read_json(plan_path)
    r._valid_plan(plan)
    if plan["workspace_id"] != workspace_id:
        raise Error("Explicit workspace does not match approved publication")
    journal = p._read_json(journal_path)
    lakehouse_id = str(uuid.UUID(journal["actions"]["create:lakehouse"]["item_id"]))
    original = p._read_json(materialize / "native-bound" / "ontology.json")
    _decode(original)
    if _decode(original) != _decode(_bound_template(
        plan["native_definition_templates"]["ontology"], lakehouse_id,
    )):
        raise Error("Materialized original ontology differs from approved bound template")
    receipt = _ownership(plan, journal, plan_path, ontology_id, original)
    compilation = p._compile(l4_run, l3_root, workspace_id, plan["name_prefix"])
    source = SealedL4ServingSource.from_run(l4_run, input_manifest_search_roots=(l3_root,))
    authority, authority_row = _publication_authority({
        "semantic_publication_authority": pq.read_table(source.resolve("semantic_publication_authority")),
    })
    definitions = copy.deepcopy(compilation.definitions)
    provenance = copy.deepcopy(compilation.provenance)
    materialized_ontology = p._read_json(materialize / "definitions" / "ontology.json")
    version_transitions = []
    for name, definition in definitions.items():
        historical = p._read_json(materialize / "definitions" / f"{name}.json")
        if (
            definition.get("publication_code_version") == "l5a-publication/1.2.0"
            and historical.get("publication_code_version") == "l5a-publication/1.1.0"
        ):
            if provenance["definition_hashes"][name] != canonical_sha256(definition):
                raise Error("Recompiled semantic definition hash is inconsistent")
            definition["publication_code_version"] = "l5a-publication/1.1.0"
            provenance["definition_hashes"][name] = canonical_sha256(definition)
            version_transitions.append(name)
    catalog_added = "presentation_catalog" not in materialized_ontology and "presentation_catalog" in definitions["ontology"]
    if catalog_added:
        # Only the explicit 1.1→1.2 compiler marker and this sealed-derived
        # catalog may differ historically. All semantic content must still
        # exactly match; never rewrite the approved plan or its hash.
        if (
            definitions["ontology"]["presentation_catalog"] != readable_catalog_from_domain(authority)
            or provenance["definition_hashes"]["ontology"] != canonical_sha256(definitions["ontology"])
        ):
            raise Error("New presentation catalog is not exactly derived from sealed Domain authority")
        definitions["ontology"].pop("presentation_catalog")
        provenance["definition_hashes"]["ontology"] = canonical_sha256(definitions["ontology"])
    if provenance != plan["provenance"] or {
        name: p._table_proof(table) for name, table in sorted(compilation.tables.items())
    } != plan["tables"]:
        raise Error("Sealed source/provenance/table proofs changed")
    if {path.stem for path in (materialize / "tables").glob("*.parquet")} != set(plan["tables"]):
        raise Error("Materialized table scope changed")
    for name, proof in plan["tables"].items():
        if p._table_proof(pq.read_table(materialize / "tables" / f"{name}.parquet")) != proof:
            raise Error("Materialized table data/binding schema changed")
    for name, definition in definitions.items():
        if p._read_json(materialize / "definitions" / f"{name}.json") != definition:
            raise Error("Materialized semantic source definition changed")
    for name, template in plan["native_definition_templates"].items():
        if p._read_json(materialize / "native-templates" / f"{name}.json") != template:
            raise Error("Immutable native template changed")
    crosswalk = p.compile_publication_crosswalk(source)
    evidence = {
        "publication_plan_bytes_hash": _digest(plan_path),
        "prototype_journal_bytes_hash": _digest(journal_path),
        "original_definition_bytes_hash": _digest(materialize / "native-bound" / "ontology.json"),
        "provenance": compilation.provenance,
        "historical_semantic_comparison": {
            "provenance": provenance,
            "verified_derived_catalog_addition_only": catalog_added,
            "verified_1_1_to_1_2_version_marker_transitions": version_transitions,
        },
        "tables": plan["tables"],
        "l4_receipt": source.receipt.model_dump(mode="json"),
        "l4_manifest": source.manifest.model_dump(mode="json"),
        "l3_input_manifest": source.input_manifest.model_dump(mode="json"),
        "source_projection": source.projection.model_dump(mode="json"),
        "approved_domain_contract": authority.model_dump(mode="json"),
        "publication_authority": dict(authority_row),
        "publication_crosswalk": crosswalk.model_dump(mode="json"),
        **_code_hashes(),
    }
    return {
        "plan": plan, "journal": journal, "original": original,
        "lakehouse_id": lakehouse_id, "reconciliation": receipt,
        "compilation": compilation, "source": source, "evidence": evidence,
    }


def _headers(response: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in response.headers.items()}


def _response_evidence(response: Any) -> dict[str, Any]:
    raw = response.content or b""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return {
        "http_status": response.status_code, "headers": _headers(response),
        "raw_body_base64": base64.b64encode(raw[:MAX_RESPONSE_BYTES]).decode("ascii"),
        "raw_body_size": len(raw), "raw_body_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_body_truncated": len(raw) > MAX_RESPONSE_BYTES,
    }


def _operation_url(headers: dict[str, str]) -> str:
    operation_id = headers.get("x-ms-operation-id")
    location = headers.get("location")
    if operation_id:
        try:
            operation_id = str(uuid.UUID(operation_id))
        except ValueError as error:
            raise Error("Invalid Fabric operation ID; retain state and inspect manually") from error
        expected = f"{p.API}/operations/{operation_id}"
        if location and location not in (expected, expected + "/result"):
            raise Error("Fabric operation Location/ID mismatch")
        return expected
    if location and re.fullmatch(re.escape(p.API) + r"/operations/[0-9a-fA-F-]{36}", location):
        uuid.UUID(location.rsplit("/", 1)[-1])
        return location
    raise Error("HTTP 202 missing a verifiable Fabric operation Location/ID")


def _retry(headers: dict[str, str]) -> None:
    try:
        delay = float(headers.get("retry-after", "1"))
    except ValueError:
        delay = 1
    time.sleep(max(0, min(delay, 30)))


class _PresentationRun(p._Run):
    def __init__(self, journal_path: Path, plan: dict[str, Any], ontology_id: str):
        if not journal_path.is_file():
            raise Error("Existing prototype journal required")
        super().__init__(journal_path, plan)
        self.ontology_id = ontology_id
        self.update_allowed = False
        self.readback_only = False
        self.reads: list[dict[str, Any]] = []
        self.definitions: dict[tuple[str, str], dict[str, Any]] = {}
        self.inventory: list[dict[str, Any]] = []
        self.event_sink = None

    @property
    def update_url(self) -> str:
        return (
            f"{p.API}/workspaces/{self.plan['workspace_id']}/ontologies/"
            f"{self.ontology_id}/updateDefinition?updateMetadata=false"
        )

    def save(self) -> None:
        pass

    def items(self) -> list[dict[str, Any]]:
        items = super().items()
        ids = [item.get("id") for item in items]
        if any(not isinstance(item_id, str) for item_id in ids) or len(set(ids)) != len(ids):
            raise Error("Invalid/duplicate workspace inventory IDs")
        self.inventory = copy.deepcopy(items)
        if self.event_sink:
            self.event_sink({"purpose": "workspace-inventory", "items": items})
        return items

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        if method == "POST" and url == self.update_url and self.update_allowed and not self.readback_only:
            self.update_allowed = False
        elif method != "GET" and not (
            method == "POST" and url.endswith("/getDefinition")
        ):
            raise Error("Presentation repair forbids create/delete and unrelated mutations")
        return super().request(method, url, **kwargs)

    def receive(self, response: Any, purpose: str) -> dict[str, Any]:
        evidence = {"purpose": purpose, **_response_evidence(response)}
        self.reads.append(evidence)
        if self.event_sink:
            self.event_sink(evidence)
        return evidence

    def poll(self, headers: dict[str, str], *, definition_result: bool) -> dict[str, Any]:
        url = _operation_url(headers)
        for _ in range(MAX_POLLS):
            _retry(headers)
            response = self.request("GET", url)
            self.receive(response, "operation-status")
            if response.status_code != 200:
                raise Error("Fabric operation status read failed; resume from retained evidence")
            body = self.checked(response)
            status = body.get("status")
            if status == "Succeeded":
                if not definition_result:
                    return body
                response = self.request("GET", url + "/result")
                self.receive(response, "definition-operation-result")
                if response.status_code != 200:
                    raise Error("Fabric definition operation result is unavailable")
                return self.checked(response)
            if status in ("Failed", "Cancelled", "Canceled"):
                raise Error(f"Fabric operation {status}; backup retained; no automatic rollback")
            if status not in ("NotStarted", "Running", "Undefined"):
                raise Error("Unknown Fabric operation state")
            headers = _headers(response)
        raise Error("Fabric operation still pending; use --resume; never repeat update")

    def definition(self, kind: str, item_id: str) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"{p.API}/workspaces/{self.plan['workspace_id']}/{p.COLLECTIONS[kind]}/{item_id}/getDefinition",
        )
        evidence = self.receive(response, f"getDefinition:{kind}:{item_id}")
        if response.status_code == 202:
            body = self.poll(evidence["headers"], definition_result=True)
        elif response.status_code == 200:
            body = self.checked(response)
        else:
            raise Error(f"Fabric definition read failed: HTTP {response.status_code}")
        definition = body.get("definition", body)
        _decode(definition)
        self.definitions[kind, item_id] = copy.deepcopy(definition)
        return definition


def _snapshot(run: _PresentationRun, context: dict[str, Any]) -> dict[str, Any]:
    proof = r._proof(
        run, "ontology", run.ontology_id, context["original"], context["lakehouse_id"],
    )
    receipt = context["reconciliation"]
    if receipt and receipt["review"]["proof"] != proof:
        raise Error("Current original definition/metadata differs from exact reconciliation proof")
    return {
        "metadata": proof["metadata"], "lakehouse_metadata": proof["lakehouse_metadata"],
        "definition": copy.deepcopy(run.definitions["ontology", run.ontology_id]),
        "ownership_proof": proof,
        "workspace_item_ids": sorted(item["id"] for item in run.inventory),
    }


def _exclusive(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise Error(f"Refusing to overwrite sealed repair evidence: {path}")
    p._atomic_json(path, payload, create=True)


def _event(state: Path, evidence: dict[str, Any]) -> None:
    directory = state / "events"
    directory.mkdir(exist_ok=True)
    _exclusive(directory / f"{len(list(directory.glob('*.json'))):06d}.json", evidence)


def _paths_safe(state: Path, inputs: dict[str, Any]) -> None:
    target = state.resolve()
    for name in ("materialize", "l4_run", "l3_root"):
        source = inputs[name].resolve()
        if target.is_relative_to(source) or source.is_relative_to(target):
            raise Error("Repair state must be separate from immutable source/materialized artifacts")
    for name in ("plan_path", "journal_path"):
        if inputs[name].resolve().is_relative_to(target):
            raise Error("Repair state cannot contain original plan/journal")
    if state.is_symlink():
        raise Error("Repair state must not be a symlink")


def repair_ontology_names(
    *, workspace_id: str, ontology_id: str, plan_path: Path, journal_path: Path,
    materialize: Path, l4_run: Path, l3_root: Path, state: Path,
    live: bool = False, approve_plan: str | None = None,
    acknowledge_nontransactional: bool = False, resume: bool = False,
    accept_verifier_update: str | None = None,
) -> dict[str, Any]:
    """Snapshot/plan by default; explicitly approve one nontransactional update."""
    workspace_id, ontology_id = str(uuid.UUID(workspace_id)), str(uuid.UUID(ontology_id))
    inputs = {
        "plan_path": plan_path, "journal_path": journal_path, "materialize": materialize,
        "l4_run": l4_run, "l3_root": l3_root,
        "workspace_id": workspace_id, "ontology_id": ontology_id,
    }
    _paths_safe(state, inputs)
    if live != bool(approve_plan and acknowledge_nontransactional) or (
        not live and (approve_plan or acknowledge_nontransactional or resume)
    ):
        raise Error("Live/resume requires --live, exact --approve-plan and --acknowledge-nontransactional")
    if accept_verifier_update is not None and not (live and resume):
        raise Error("--accept-verifier-update requires approved --live --resume; it can NEVER initiate an update")
    if not live:
        if state.exists():
            raise Error("Plan state already exists; choose a NEW directory (never overwrite sealed plans)")
        context = _local(**inputs)
        run = _PresentationRun(journal_path, context["plan"], ontology_id)
        snapshot = _snapshot(run, context)
        replacement, mapping = _render(snapshot["definition"], context["compilation"], context["source"])
        changes = presentation_diff(snapshot["definition"], replacement)
        invariants = _invariants(snapshot["definition"])
        if _invariants(replacement) != invariants:
            raise Error("Presentation renderer changed identity/topology/binding invariants")
        if not changes:
            raise Error("No presentation changes; original snapshot is already readable")
        if _local(**inputs)["evidence"] != context["evidence"]:
            raise Error("Source/code changed while planning; no plan sealed")
        mapping_sidecar = {
            "policy": POLICY,
            "authority": "sealed-L4-approved-Domain-and-publication-crosswalk",
            "domain_contract_hash": context["evidence"]["publication_authority"]["domain_contract_hash"],
            "crosswalk_hash": context["plan"]["provenance"]["crosswalk_hash"],
            "native_name_notice": (
                "Fabric names require bounded native identifiers; spaces/punctuation may become underscores. "
                "Exact approved human labels and canonical IDs are retained in mappings, not added as live synonyms."
            ),
            "mappings": mapping, "allowed_field_diff": changes,
        }
        plan = {
            "policy": POLICY, "workspace_id": workspace_id, "ontology_id": ontology_id,
            "lakehouse_id": context["lakehouse_id"],
            "operation": run.update_url, "method": "POST",
            "concurrency": "operator-approved-nontransactional; no CAS/ETag; residual race remains",
            "allowed_effect": "names/descriptions/instance-display-property only; no create/delete/data/type/scope changes",
            "rollback": "manual review of retained full backup only; never automatic overwrite",
            "inputs": {key: str(value.resolve()) if isinstance(value, Path) else value for key, value in inputs.items()},
            "local_evidence": context["evidence"],
            "lineage": {
                "publication_plan_hash": context["plan"]["plan_hash"],
                "publication_plan": context["plan"],
                "prototype_journal": context["journal"],
                "reconciliation": context["reconciliation"],
            },
            "backup_hash": canonical_sha256(snapshot),
            "before_hash": _content_hash(snapshot["definition"]),
            "after_hash": _content_hash(replacement),
            "replacement_hash": canonical_sha256(replacement),
            "mapping": mapping, "allowed_field_diff": changes,
            "mapping_sidecar_hash": canonical_sha256(mapping_sidecar),
            "unchanged_invariants": invariants,
            "scope": context["plan"]["provenance"].get("window_run_scope"),
            "companion_graph": "not updated; independent graph/publication readiness must be reassessed",
        }
        plan["plan_hash"] = canonical_sha256(plan)
        state.mkdir(parents=True, exist_ok=False)
        _exclusive(state / "backup.json", snapshot)
        _exclusive(state / "replacement.json", replacement)
        _exclusive(state / "mapping.json", mapping_sidecar)
        _exclusive(state / "plan.json", plan)
        _exclusive(state / "read-evidence.json", {"responses": run.reads})
        return {
            "status": "planned-read-only", "plan_hash": plan["plan_hash"],
            "state": str(state), "ontology_id": ontology_id, "lakehouse_id": context["lakehouse_id"],
            "changed_fields": len(changes), "before_hash": plan["before_hash"], "after_hash": plan["after_hash"],
            "mapping_sidecar": str(state / "mapping.json"),
        }
    if not state.is_dir():
        raise Error("Existing sealed repair --state required")
    with (state / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("Repair state already active") from None
        plan = p._read_json(state / "plan.json")
        if (
            plan.get("policy") != POLICY or plan.get("plan_hash") != approve_plan
            or canonical_sha256({key: value for key, value in plan.items() if key != "plan_hash"}) != approve_plan
            or plan["inputs"] != {
                key: str(value.resolve()) if isinstance(value, Path) else value for key, value in inputs.items()
            }
        ):
            raise Error("Exact repair approval/immutable plan/input identity mismatch")
        snapshot, replacement = p._read_json(state / "backup.json"), p._read_json(state / "replacement.json")
        if (
            canonical_sha256(snapshot) != plan["backup_hash"]
            or _content_hash(snapshot["definition"]) != plan["before_hash"]
            or canonical_sha256(replacement) != plan["replacement_hash"]
            or _content_hash(replacement) != plan["after_hash"]
            or presentation_diff(snapshot["definition"], replacement) != plan["allowed_field_diff"]
            or _invariants(replacement) != plan["unchanged_invariants"]
            or _invariants(snapshot["definition"]) != plan["unchanged_invariants"]
            or canonical_sha256(p._read_json(state / "mapping.json")) != plan["mapping_sidecar_hash"]
        ):
            raise Error("Sealed backup/replacement/mapping changed")
        context = _local(**inputs)
        verifier_upgrade = None
        if accept_verifier_update is not None:
            verifier_upgrade = _verifier_upgrade(state, plan, context, replacement, accept_verifier_update)
        elif context["evidence"] != plan["local_evidence"]:
            raise Error("Source/compiler/naming policy/journal drift; create a fresh reviewed plan")
        rendered, mapping = _render(snapshot["definition"], context["compilation"], context["source"])
        if rendered != replacement or mapping != plan["mapping"]:
            raise Error("Fresh authoritative presentation mapping differs from approved plan")
        run = _PresentationRun(journal_path, context["plan"], ontology_id)
        run.readback_only = accept_verifier_update is not None
        run.event_sink = lambda evidence: _event(state, evidence)
        if verifier_upgrade:
            _event(state, {"purpose": "explicit-readback-only-verifier-upgrade", **verifier_upgrade})
        intent_path = state / "update-intent.json"
        if intent_path.exists():
            if not resume:
                raise Error("Update intent already exists; use --resume; never repeat POST")
            intent = p._read_json(intent_path)
            if intent != {
                "plan_hash": approve_plan, "operation": run.update_url,
                "request_hash": canonical_sha256({"definition": replacement}),
                "status": "intent-durable-before-single-post",
            }:
                raise Error("Update intent does not match approved repair")
            response_path = state / "update-response.json"
            actual = run.definition("ontology", ontology_id)
            if _content_hash(actual) == plan["before_hash"] and response_path.exists():
                response = p._read_json(response_path)
                if response["http_status"] == 202:
                    run.poll(response["headers"], definition_result=False)
            return _verify(
                run, context, snapshot, replacement, plan, state, resumed=True,
                verifier_upgrade=verifier_upgrade,
            )
        if resume:
            raise Error("No update intent exists; --resume will not initiate an update")
        if (state / "receipt.json").exists():
            raise Error("Receipt without update intent; refuse inconsistent state")
        if _snapshot(run, context) != snapshot:
            raise Error("Fresh remote metadata/definition changed; no update issued")
        if _local(**inputs)["evidence"] != plan["local_evidence"]:
            raise Error("Source/compiler changed immediately before update")
        _exclusive(intent_path, {
            "plan_hash": approve_plan, "operation": run.update_url,
            "request_hash": canonical_sha256({"definition": replacement}),
            "status": "intent-durable-before-single-post",
        })
        run.update_allowed = True
        try:
            response = run.request("POST", run.update_url, json={"definition": replacement})
        except Exception as error:
            raise Error(
                "Unknown update outcome; durable intent/backup retained. "
                "Use --resume for readback only; NEVER repeat POST or automatically roll back."
            ) from error
        evidence = _response_evidence(response)
        _exclusive(state / "update-response.json", evidence)
        if response.status_code == 202:
            run.poll(evidence["headers"], definition_result=False)
        elif response.status_code != 200:
            raise Error(f"Update HTTP {response.status_code}; retained evidence; never retry/rollback automatically")
        return _verify(run, context, snapshot, replacement, plan, state, resumed=False)


def _verifier_upgrade(
    state: Path, plan: dict[str, Any], context: dict[str, Any],
    replacement: dict[str, Any], accepted_hash: str,
) -> dict[str, Any]:
    previous = plan["local_evidence"].get("repair_code_hash")
    current = context["evidence"].get("repair_code_hash")
    if (
        not isinstance(previous, str) or not re.fullmatch(r"[0-9a-f]{64}", previous)
        or not isinstance(current, str) or not re.fullmatch(r"[0-9a-f]{64}", current)
        or accepted_hash != current or previous == current
    ):
        raise Error("Verifier upgrade requires the exact CURRENT repair code hash and a valid different recorded hash")
    comparison = copy.deepcopy(context["evidence"])
    comparison["repair_code_hash"] = previous
    if comparison != plan["local_evidence"]:
        raise Error("Verifier upgrade permits ONLY repair_code_hash drift; compiler/naming/source/journal must match")
    operation = (
        f"{p.API}/workspaces/{plan['workspace_id']}/ontologies/"
        f"{plan['ontology_id']}/updateDefinition?updateMetadata=false"
    )
    intent_path, response_path = state / "update-intent.json", state / "update-response.json"
    if not intent_path.is_file() or not response_path.is_file():
        raise Error("Readback-only verifier upgrade requires durable matching update intent AND response")
    if p._read_json(intent_path) != {
        "plan_hash": plan["plan_hash"], "operation": operation,
        "request_hash": canonical_sha256({"definition": replacement}),
        "status": "intent-durable-before-single-post",
    }:
        raise Error("Verifier upgrade intent does not match the original approved update")
    response = p._read_json(response_path)
    if (
        not isinstance(response, dict)
        or response.get("http_status") not in (200, 202)
        or not isinstance(response.get("headers"), dict)
        or type(response.get("raw_body_size")) is not int
        or response["raw_body_size"] < 0
        or not isinstance(response.get("raw_body_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", response["raw_body_sha256"])
        or type(response.get("raw_body_truncated")) is not bool
    ):
        raise Error("Verifier upgrade requires valid durable successful/accepted response evidence")
    try:
        raw = base64.b64decode(response["raw_body_base64"], validate=True)
    except (ValueError, TypeError, KeyError) as error:
        raise Error("Invalid durable update response body evidence") from error
    if (
        len(raw) != min(response["raw_body_size"], MAX_RESPONSE_BYTES)
        or response["raw_body_truncated"] != (response["raw_body_size"] > MAX_RESPONSE_BYTES)
        or not response["raw_body_truncated"] and hashlib.sha256(raw).hexdigest() != response["raw_body_sha256"]
    ):
        raise Error("Durable update response body evidence is inconsistent")
    if response["http_status"] == 202:
        _operation_url(response["headers"])
    return {
        "authority": "explicit-operator-approved-readback-only-verifier-upgrade",
        "original_plan_hash": plan["plan_hash"],
        "original_repair_code_hash": previous,
        "accepted_current_repair_code_hash": accepted_hash,
        "readback_policy": READBACK_POLICY,
        "unchanged_non_verifier_evidence_hash": canonical_sha256({
            key: value for key, value in comparison.items() if key != "repair_code_hash"
        }),
        "durable_intent_bytes_hash": _digest(intent_path),
        "durable_response_bytes_hash": _digest(response_path),
        "update_post_authorized": False,
    }


def _verify(
    run: _PresentationRun, context: dict[str, Any], snapshot: dict[str, Any],
    replacement: dict[str, Any], plan: dict[str, Any], state: Path, *, resumed: bool,
    verifier_upgrade: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = r._identity(run, "ontology", run.ontology_id)
    actual = run.definition("ontology", run.ontology_id)
    lakehouse = r._identity(run, "lakehouse", context["lakehouse_id"])
    if (
        metadata != snapshot["metadata"]
        or lakehouse != snapshot["lakehouse_metadata"]
        or r._identity(run, "ontology", run.ontology_id) != metadata
    ):
        raise Error("Readback metadata/sensitivity/Lakehouse changed; not successful; backup retained")
    actual_hash = _content_hash(actual)
    if resumed and actual_hash == plan["before_hash"]:
        raise Error("Unknown update outcome: original still present; NEVER repeat POST; inspect manually")
    equivalence = _readback_equivalence(replacement, actual)
    if equivalence["normalized_comparison_hash"] != plan["after_hash"]:
        raise Error("Readback comparison does not match the original approved after hash")
    inventory = sorted(item["id"] for item in run.items())
    if inventory != snapshot["workspace_item_ids"]:
        raise Error("Workspace resource inventory changed; inspect service companions/concurrent edits; backup retained")
    if verifier_upgrade:
        fresh_inputs = {
            key: value if key in ("workspace_id", "ontology_id") else Path(value)
            for key, value in plan["inputs"].items()
        }
        if (
            _local(**fresh_inputs)["evidence"] != context["evidence"]
            or _digest(state / "update-intent.json") != verifier_upgrade["durable_intent_bytes_hash"]
            or _digest(state / "update-response.json") != verifier_upgrade["durable_response_bytes_hash"]
        ):
            raise Error("Verifier/source/durable proof changed during readback; no receipt sealed")
    receipt = {
        "policy": POLICY, "status": "presentation-verified-same-item",
        "plan_hash": plan["plan_hash"], "ontology_id": run.ontology_id,
        "lakehouse_id": context["lakehouse_id"], "definition_hash": actual_hash,
        "metadata": metadata, "lakehouse_metadata": lakehouse,
        "readback_equivalence": equivalence, "verifier_upgrade": verifier_upgrade,
        "publication_snapshot": {
            "status": "SUPERSEDED",
            "original_publication_plan_hash": context["plan"]["plan_hash"],
            "original_definition_hash": plan["before_hash"],
            "reason": "Presentation definition changed by separately approved same-item repair",
            "old_readiness": "not-current; do not blindly resume original publisher",
        },
        "scope": plan["scope"], "companion_graph": plan["companion_graph"],
        "resources_created": 0, "resources_deleted": 0,
        "workspace_item_ids": inventory,
        "backup": str(state / "backup.json"),
        "mapping_sidecar": str(state / "mapping.json"),
        "business_readiness": "not reassessed; source bounds and evidence obligations unchanged",
    }
    if (state / "receipt.json").exists():
        if p._read_json(state / "receipt.json") != receipt:
            raise Error("Existing immutable repair receipt differs from verified readback")
    else:
        verified_path = state / "verified-definition.json"
        if verified_path.exists():
            if _content_hash(p._read_json(verified_path)) != actual_hash:
                raise Error("Interrupted verification snapshot differs from fresh readback")
        else:
            _exclusive(verified_path, actual)
        _exclusive(state / "receipt.json", receipt)
    return receipt
