"""Explicit, nontransactional, create-only publication of sealed Schema-2 data.

This path deliberately does not implement the transactional L5a target interface.
Its journal owns returned IDs, not names; uncertain writes stop rather than retry.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.deploy.fabric_ontology_definition import (
    BASE_ENTITY_TYPE_ID,
    compile_fabric_ontology_definition,
    instance_base_entity_table,
)
from fabric_kg_builder.deploy.fabric_semantic_model_definition import (
    compile_fabric_semantic_model_definition,
)
from fabric_kg_builder.deploy.lakehouse_schema import (
    onelake_tables_path,
    resolve_lakehouse_schema,
)
from fabric_kg_builder.deploy.ontology_names import (
    METADATA_ONLY_ALIASES_LIMITATION,
    NATIVE_NAME_PATTERN,
    allocate_readable_names,
)
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
from fabric_kg_builder.serving.graph_model import (
    build_graph_model_parts,
    encode_parts_for_api,
    validate_graph_data_source_paths,
    validate_graph_model_schema,
)
from fabric_kg_builder.serving.l5a_crosswalk import (
    compile_access_policy,
    compile_governed_assets,
    compile_publication_crosswalks,
    publication_crosswalk_set_hash,
)
from fabric_kg_builder.serving.structured_publication import (
    _typed_fingerprint_scalar,
    _typed_table_fingerprint_rows,
    compile_l5a_publication,
    export_serving_question_context,
)

FORMAT_VERSION = "1.0.0"
MAX_GRAPH_READBACK_ROWS = 1000
DEFAULT_GRAPH_READBACK_TOTAL_ROWS = 1_000_000
API = "https://api.fabric.microsoft.com/v1"
LAKEHOUSE_REFERENCE = "${created.lakehouse.id}"
COLLECTIONS = {
    "lakehouse": "lakehouses",
    "ontology": "ontologies",
    "graph": "graphModels",
    "semantic_model": "semanticModels",
}
ITEM_TYPES = {
    "lakehouse": "Lakehouse",
    "ontology": "Ontology",
    "graph": "GraphModel",
    "semantic_model": "SemanticModel",
}
GRAPH_TYPES = {
    "string": "STRING",
    "integer": "INT",
    "number": "DOUBLE",
    "boolean": "BOOLEAN",
    "datetime": "ZONED DATETIME",
}


class PrototypePublicationError(ValueError):
    """Publication stopped without rollback, replacement or automatic retry."""


@dataclass
class _Compilation:
    definitions: dict[str, Any]
    tables: dict[str, Any]
    graph_parts: list[dict[str, Any]]
    graph_catalog: dict[str, Any]
    limitations: list[str]
    blockers: list[str]
    provenance: dict[str, Any]


@dataclass
class _GraphReadbackCheck:
    match: str
    returns: str
    rows: list[dict[str, Any]]
    fields: list[Any]
    keys: list[tuple[str, str]]

    def windows(self, page_size: int) -> Any:
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows:
            key = tuple(row[column] for _, column in self.keys)
            if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:_.-]+", value) for value in key):
                raise PrototypePublicationError("Readback key is not a safe immutable canonical ID")
            groups[key].append(row)
        pending: list[dict[str, Any]] = []
        lower = upper = None
        for key, rows in sorted(groups.items()):
            if len(rows) > page_size:
                raise PrototypePublicationError(
                    "One canonical endpoint-pair bucket exceeds the page size and exposes no unique edge ID; "
                    "cannot safely keyset-page this bucket"
                )
            if pending and len(pending) + len(rows) > page_size:
                yield self._window(lower, upper, pending)
                pending, lower = [], None
            if lower is None:
                lower = key
            upper = key
            pending.extend(rows)
        if pending or not groups:
            yield self._window(lower, upper, pending)

    def _window(self, lower: Any, upper: Any, rows: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        predicates = ""
        if lower is not None:
            expressions = [expression for expression, _ in self.keys]
            if len(expressions) == 1:
                predicates = f" WHERE {expressions[0]} >= '{lower[0]}' AND {expressions[0]} <= '{upper[0]}'"
            else:
                first, second = expressions
                predicates = (
                    f" WHERE ({first} > '{lower[0]}' OR ({first} = '{lower[0]}' AND {second} >= '{lower[1]}'))"
                    f" AND ({first} < '{upper[0]}' OR ({first} = '{upper[0]}' AND {second} <= '{upper[1]}'))"
                )
        return f"MATCH {self.match}{predicates} RETURN {self.returns} LIMIT {len(rows) + 1}", rows


def _atomic_json(path: Path, payload: dict[str, Any], *, create: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (canonical_json(payload) + "\n").encode("utf-8")
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with staging.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            try:
                os.link(staging, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise PrototypePublicationError(
                        f"Refusing to replace existing artifact: {path}"
                    ) from None
        else:
            os.replace(staging, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staging.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PrototypePublicationError(f"Expected an object in {path}")
    return value


def _table_proof(table: Any) -> dict[str, Any]:
    # Delta does not promise row order. Bind the full typed row multiset instead.
    rows = sorted(canonical_json(row) for row in _typed_table_fingerprint_rows(table))
    return {
        "schema": [
            {"name": field.name, "type": str(field.type), "nullable": field.nullable}
            for field in table.schema
        ],
        "row_count": table.num_rows,
        "values_hash": canonical_sha256(rows),
    }


def _compile(
    l4_run: Path, l3_root: Path, workspace_id: str, prefix: str,
    *, quality_policy: dict[str, Any] | None = None,
) -> _Compilation:
    import pyarrow as pa

    source = SealedL4ServingSource.from_run(
        l4_run, input_manifest_search_roots=(l3_root,)
    )
    targets = {
        kind: f"prototype-target:{prefix}:{kind}"
        for kind in ("parquet", "semantic_model", "ontology", "graph")
    }
    crosswalks = compile_publication_crosswalks(source)
    policy = compile_access_policy(
        source,
        access_policy_id=f"access-policy:{prefix}",
        principal_id=f"principal:{prefix}-publisher",
        resource_scope_id=f"resource:fabric-workspace:{workspace_id}",
        authorization_resource_id=f"authorization-resource:{prefix}",
    )
    assets = compile_governed_assets(
        source, crosswalks=crosswalks, access_policy=policy,
        target_ids=targets, workspace_id=workspace_id,
        quality_policy=quality_policy,
    )
    compiled = compile_l5a_publication(
        source, crosswalks=crosswalks, access_policy=policy,
        governed_assets=assets, target_ids=targets,
        quality_policy=quality_policy,
    )
    question_context = export_serving_question_context(source)
    window_scope = question_context.get("window_run_scope")
    partial_scope = question_context.get("partial_extraction_scope")
    tables = dict(compiled.tables)
    if tables["l4_semantic_asserted_entities"].num_rows == 0:
        raise PrototypePublicationError("Empty asserted semantic entity data is not publishable")
    limitations: list[str] = []
    blockers: list[str] = []
    graph = compiled.definitions["graph"]
    nodes = {
        item["canonical_semantic_type_id"]: item for item in graph["node_types"]
    }
    presentation = compiled.definitions["ontology"]["presentation_catalog"]
    labels = allocate_readable_names({
        semantic_id: presentation["entity_types"][semantic_id]
        for semantic_id in nodes
    })
    identities: dict[str, set[str]] = {}
    bindings: dict[str, dict[str, Any]] = {}
    types_by_alias: dict[str, dict[str, str]] = {}
    for semantic_id, node in sorted(nodes.items()):
        table_id = node["physical_table_id"]
        table = tables[table_id]
        identity_column = node["physical_identity_column"]
        identities[semantic_id] = set(table[identity_column].to_pylist())
        alias = node["aliases"][0] if node["aliases"] else node["label"] + "_nodeType"
        columns = {identity_column: "STRING", "__label": "STRING"}
        for prop in node["properties"]:
            native_type = GRAPH_TYPES.get(prop["data_type"])
            if native_type is None:
                blockers.append(
                    f"native-type-unsupported:{prop['canonical_property_id']}:{prop['data_type']}"
                )
                continue
            columns[prop["physical_column_id"]] = native_type
        bindings[semantic_id] = {
            "table": table_id, "entity_id_column": identity_column,
            "node_type_alias": alias, "property_columns": list(columns),
        }
        types_by_alias[alias] = columns

    pairs: list[dict[str, Any]] = []
    edge_catalog: list[dict[str, Any]] = []
    edge_presentation: dict[str, dict[str, Any]] = {}
    for edge in graph["edge_types"]:
        relationship_id = edge["canonical_semantic_relationship_id"]
        source_types = edge["allowed_source_semantic_type_ids"]
        target_types = edge["allowed_target_semantic_type_ids"]
        if not source_types or not target_types:
            raise PrototypePublicationError(f"Empty endpoint authority: {relationship_id}")
        expanded = len(source_types) * len(target_types) > 1
        if expanded:
            limitations.extend((
                f"ontology.endpoint-widening:{relationship_id}",
                f"graph.endpoint-membership-expansion:{relationship_id}",
            ))
        if edge["direction"] not in ("directed", "source_to_target"):
            limitations.append(
                f"native.directed-relationship:{relationship_id}:{edge['direction']}"
            )
        table = tables[edge["physical_table_id"]]
        rows = table.to_pylist()
        covered: set[int] = set()
        for source_type in source_types:
            for target_type in target_types:
                if source_type not in nodes or target_type not in nodes:
                    raise PrototypePublicationError(
                        f"Unmapped endpoint in {relationship_id}"
                    )
                selected = [
                    index for index, row in enumerate(rows)
                    if row[edge["source_identity_column"]] in identities[source_type]
                    and row[edge["target_identity_column"]] in identities[target_type]
                ]
                covered.update(selected)
                suffix = canonical_sha256(
                    [relationship_id, source_type, target_type]
                )[:16]
                table_id = edge["physical_table_id"]
                metadata = presentation["relationship_types"][relationship_id]
                if expanded:
                    table_id = f"prototype_graph_edge_{suffix}"
                    if table_id in tables:
                        raise PrototypePublicationError("Derived graph table collision")
                    tables[table_id] = table.take(pa.array(selected, type=pa.int64()))
                    metadata = {
                        "display_name": (
                            f"{labels[source_type]} {metadata['display_name']} {labels[target_type]}"
                        ),
                    }
                edge_presentation[f"edge_{suffix}"] = metadata
                columns = [
                    "__canonical_id", "__semantic_relationship_id",
                    edge["source_identity_column"], edge["target_identity_column"],
                ]
                pairs.append({
                    "name": relationship_id,
                    "source_type": source_type, "target_type": target_type,
                    "table": table_id, "property_columns": columns,
                    "source_entity_id_column": edge["source_identity_column"],
                    "target_entity_id_column": edge["target_identity_column"],
                    "graph_alias": f"edge_{suffix}",
                })
                edge_catalog.append({
                    "canonical_relationship_id": relationship_id,
                    "source_type": source_type, "target_type": target_type,
                    "physical_table_id": table_id,
                    "source_table_id": edge["physical_table_id"],
                    "row_count": len(selected),
                })
        if covered != set(range(table.num_rows)):
            raise PrototypePublicationError(
                f"Graph endpoint bindings would discard asserted edges: {relationship_id}"
            )
    if not sum(tables[node["physical_table_id"]].num_rows for node in nodes.values()):
        raise PrototypePublicationError("No typed semantic data to bind")
    edge_labels = allocate_readable_names(
        edge_presentation, prefix="Relationship", reserved=list(labels.values()),
    )
    for pair, entry in zip(pairs, edge_catalog):
        pair["graph_label"] = entry["graph_label"] = edge_labels[pair["graph_alias"]]
    parts = build_graph_model_parts(
        entity_types=sorted(nodes), relationship_pairs=pairs,
        node_labels=labels, node_table_bindings=bindings,
        workspace_id=workspace_id, lakehouse_item_id=LAKEHOUSE_REFERENCE,
        schema="dbo", model_name="schema2_prototype",
    )
    parts = [part for part in parts if part["path"] != ".platform"]
    node_source_columns = {
        binding["nodeTypeAlias"]: {
            item["propertyName"]: item["sourceColumn"]
            for item in binding["propertyMappings"]
        }
        for part in parts if part["path"] == "graphDefinition.json"
        for binding in part["payload_json"]["nodeTables"]
    }
    for part in parts:
        if part["path"] == "graphType.json":
            for node in part["payload_json"]["nodeTypes"]:
                for prop in node["properties"]:
                    column = node_source_columns[node["alias"]][prop["name"]]
                    prop["type"] = types_by_alias[node["alias"]][column]
    validate_graph_data_source_paths(parts)
    validate_graph_model_schema(parts)
    for table_id in tables:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_id):
            raise PrototypePublicationError(f"Unsafe physical table name: {table_id}")
    ontology = compiled.definitions["ontology"]
    if ontology["relationship_types"] or any(item["properties"] for item in ontology["entity_types"]):
        limitations.append(METADATA_ONLY_ALIASES_LIMITATION)
    return _Compilation(
        definitions=compiled.definitions, tables=tables, graph_parts=parts,
        graph_catalog={
            "nodes": [
                {"canonical_type_id": semantic_id, "graph_label": labels[semantic_id],
                 "physical_table_id": node["physical_table_id"],
                 "row_count": tables[node["physical_table_id"]].num_rows}
                for semantic_id, node in sorted(nodes.items())
            ],
            "edges": edge_catalog,
            "identity_semantics": "one node per canonical-id/semantic-type membership",
            "expected_node_count": sum(
                tables[node["physical_table_id"]].num_rows for node in nodes.values()
            ),
            "expected_edge_count": sum(item["row_count"] for item in edge_catalog),
        },
        limitations=limitations, blockers=blockers,
        provenance={
            **({"window_run_scope": window_scope} if window_scope is not None else {}),
            **({"partial_extraction_scope": partial_scope} if partial_scope is not None else {}),
            "source_projection_id": compiled.definitions["parquet"]["source_projection_id"],
            "source_projection_hash": compiled.definitions["parquet"]["source_projection_hash"],
            "crosswalk_hash": publication_crosswalk_set_hash(crosswalks),
            **({"crosswalk_hashes": sorted(item.crosswalk_hash for item in crosswalks)}
               if len(crosswalks) > 1 else {}),
            "stable_id_lock_hash": crosswalks[0].stable_id_lock_hash,
            "access_policy_hash": policy.policy_hash,
            "definition_hashes": {
                kind: canonical_sha256(value)
                for kind, value in sorted(compiled.definitions.items())
            },
        },
    )


def _compile_from_plan(
    l4_run: Path, l3_root: Path, plan: dict[str, Any],
) -> _Compilation:
    """Retain publication quality authority through readback and repair paths."""
    if "business_quality" not in plan:
        return _compile(l4_run, l3_root, plan["workspace_id"], plan["name_prefix"])
    report = plan["business_quality"]
    if not isinstance(report, dict) or not isinstance(report.get("policy"), dict):
        raise PrototypePublicationError("Bound business quality policy is missing")
    compilation = _compile(
        l4_run, l3_root, plan["workspace_id"], plan["name_prefix"],
        quality_policy=report["policy"],
    )
    if compilation.definitions["ontology"].get("business_quality") != report:
        raise PrototypePublicationError("Immutable business quality assessment changed; create a new plan")
    return compilation


def _prototype_description(
    run_id: str, window_scope: dict[str, Any] | None = None,
    partial_scope: dict[str, Any] | None = None,
) -> str:
    if partial_scope is not None:
        plan = partial_scope["plan"]
        inventory = plan["source_chunk_inventory_count"]
        corpus = (
            f"{inventory} chunks" if inventory is not None
            else f"{plan['source_unit_inventory_count']} source units"
        )
        roots = (
            f"PARTIAL: roots={plan['selected_root_count']}/{plan['approved_contract_root_count']} "
            f"selected,{plan['operator_excluded_completed_root_count']} completed-excluded,"
            f"{plan['missing_root_count']} missing; "
            if plan.get("operator_excluded_completed_root_count", 0) else
            f"PARTIAL EXTRACTION: approved roots={plan['completed_root_count']}/"
            f"{plan['approved_contract_root_count']} complete,{plan['excluded_root_count']} excluded; "
        )
        description = (
            roots +
            f"leaves={plan['completed_leaf_count']}; corpus={corpus}; NOT full coverage; "
            f"scope={plan['plan_hash']}; run={run_id}"
        )
        if len(description) > 256:
            raise PrototypePublicationError(
                "Scoped prototype description exceeds 256 characters; no content was truncated"
            )
        return description
    if window_scope is None:
        return f"Schema2 create-only prototype run {run_id}; retain partial items"
    label = (
        "LIMITED PREFIX" if window_scope["authority"] == "limited_committed_prefix_only"
        else "PARTIAL COVERAGE"
    )
    description = (
        f"{label} {window_scope['selected_chunk_count']}/{window_scope['total_chunk_count']} "
        f"({window_scope['omitted_chunk_count']} excluded); NOT full coverage; "
        f"scope={window_scope['acceptance_hash']}; "
        f"Schema2 create-only prototype run {run_id}; retain partial items"
    )
    if len(description) > 256:
        raise PrototypePublicationError("Scoped prototype description exceeds 256 characters; no content was truncated")
    return description


def _ontology_parts(
    compilation: _Compilation, workspace_id: str, lakehouse_id: str,
    name: str, description: str,
    *, legacy_names: bool = False,
) -> list[dict[str, str]]:
    native = compile_fabric_ontology_definition(
        compilation.definitions["ontology"], workspace_id=workspace_id,
        lakehouse_id=lakehouse_id, display_name=name, description=description,
        lakehouse="dbo", legacy_names=legacy_names,
    )
    parts = [part for part in native.parts if part["path"] != ".platform"]
    if len({part["path"] for part in parts}) != len(parts):
        raise PrototypePublicationError("Native Ontology part/ID collision")
    # The legacy compiler always emits a generic Surface base. Without widening,
    # that extra semantic type is unnecessary and is not part of this authority.
    if not native.widened_relationships:
        parts = [
            part for part in parts
            if not part["path"].startswith(f"EntityTypes/{BASE_ENTITY_TYPE_ID}/")
        ]
    for part in parts:
        if part["path"].endswith("/definition.json"):
            payload = json.loads(base64.b64decode(part["payload"]))
            for item in [payload, *payload.get("properties", [])]:
                name_value = item.get("name")
                if name_value is not None and not re.fullmatch(
                    NATIVE_NAME_PATTERN, name_value
                ):
                    raise PrototypePublicationError(
                        f"Invalid native Ontology name {name_value!r}; "
                        "requires an explicit wire-name compiler mapping"
                    )
    return parts


def _materialize(compilation: _Compilation, root: Path) -> None:
    import pyarrow.parquet as pq

    unexpected = {
        path.stem for path in (root / "tables").glob("*.parquet")
    } - set(compilation.tables)
    if unexpected:
        raise PrototypePublicationError(
            f"Materialization contains tables outside sealed compilation: {sorted(unexpected)}"
        )
    for table_id, table in sorted(compilation.tables.items()):
        path = root / "tables" / f"{table_id}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if _table_proof(pq.read_table(path)) != _table_proof(table):
                raise PrototypePublicationError(f"Materialized data changed: {path}")
            continue
        staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
        try:
            pq.write_table(table, staging, compression="snappy", version="2.6")
            with staging.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(staging, path)
        finally:
            staging.unlink(missing_ok=True)
    for kind, definition in compilation.definitions.items():
        _atomic_json(root / "definitions" / f"{kind}.json", definition, create=True)


def _native_definitions(
    compilation: _Compilation, *, workspace_id: str, lakehouse_id: str,
    names: dict[str, str], description: str, root: Path,
    semantic_model: bool,
) -> tuple[dict[str, Any], list[str]]:
    definitions = {
        "ontology": {"parts": _ontology_parts(
            compilation, workspace_id, lakehouse_id, names["ontology"], description
        )},
        "graph": {"parts": encode_parts_for_api(json.loads(
            json.dumps(compilation.graph_parts).replace(
                LAKEHOUSE_REFERENCE, lakehouse_id
            )
        ))},
    }
    exclusions: list[str] = []
    if semantic_model:
        sm = compile_fabric_semantic_model_definition(
            tables_root=root / "tables", workspace_id=workspace_id,
            lakehouse_id=lakehouse_id, lakehouse="dbo",
        )
        definitions["semantic_model"] = {"parts": sm.parts}
        exclusions = [
            f"semantic-model.excluded-column:{item.table_name}.{item.column_name}:{item.arrow_type}"
            for item in sm.excluded_columns
        ]
        if compilation.definitions["ontology"]["relationship_types"]:
            exclusions.append("semantic-model.relationships-not-compiled")
    return definitions, exclusions


def _compiler_hash() -> str:
    from fabric_kg_builder.deploy import fabric_ontology_definition
    from fabric_kg_builder.deploy import ontology_names
    from fabric_kg_builder.deploy import fabric_semantic_model_definition
    from fabric_kg_builder.serving import business_quality, graph_model, structured_publication

    return canonical_sha256({
        module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for module in (
            fabric_ontology_definition, ontology_names, fabric_semantic_model_definition,
            graph_model, structured_publication, business_quality,
        )
    } | {
        "prototype": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "prototype_reconciliation": hashlib.sha256(
            Path(__file__).with_name("schema2_prototype_reconcile.py").read_bytes()
        ).hexdigest(),
    })


class _Run:
    def __init__(self, path: Path, plan: dict[str, Any]) -> None:
        self.path = path
        self.plan = plan
        if path.exists():
            self.data = _read_json(path)
            if (
                self.data.get("plan_hash") != plan["plan_hash"]
                or self.data.get("run_id") != plan["run_id"]
                or self.data.get("journal_version") != FORMAT_VERSION
            ):
                raise PrototypePublicationError("Journal does not own this exact approved plan")
        else:
            self.data = {
                "journal_version": FORMAT_VERSION, "plan_hash": plan["plan_hash"],
                "run_id": plan["run_id"], "workspace_id": plan["workspace_id"],
                "policy": "create-only-retain-partial", "status": "planned",
                "actions": {}, "read_operations": [], "observed_new_items": [],
                "business_acceptance": "pending-six-source-cited-question-results",
            }
            _atomic_json(path, self.data, create=True)
        from azure.identity import AzureCliCredential
        self.credential = AzureCliCredential()

    def save(self) -> None:
        _atomic_json(self.path, self.data)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        import requests

        if not url.startswith(API + "/"):
            raise PrototypePublicationError("Refusing a cross-origin Fabric operation URL")
        token = self.credential.get_token(API.removesuffix("/v1") + "/.default").token
        return requests.request(
            method, url, headers={"Authorization": f"Bearer {token}"},
            timeout=90, allow_redirects=False, **kwargs,
        )

    @staticmethod
    def checked(response: Any) -> dict[str, Any]:
        if response.status_code not in (200, 201, 202):
            try:
                code = response.json().get("errorCode", "unknown")
            except (ValueError, AttributeError):
                code = "non-json-error"
            raise PrototypePublicationError(
                f"Fabric HTTP {response.status_code}; errorCode={str(code)[:120]}"
            )
        body = response.json() if response.content else {}
        if body is None and response.status_code == 202:
            return {}
        if not isinstance(body, dict):
            raise PrototypePublicationError("Fabric response body must be an object (or null for HTTP 202)")
        return body

    def items(self) -> list[dict[str, Any]]:
        url = f"{API}/workspaces/{self.plan['workspace_id']}/items"
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        while url:
            if url in seen:
                raise PrototypePublicationError("Repeated Fabric pagination URL")
            seen.add(url)
            body = self.checked(self.request("GET", url))
            items.extend(body.get("value", []))
            url = body.get("continuationUri")
            if body.get("continuationToken") and not url:
                raise PrototypePublicationError("Unresolved Fabric pagination token")
        return items

    def _receive(self, response: Any, action: dict[str, Any]) -> dict[str, Any]:
        # Persist the transport evidence before decoding an untrusted response.
        action.update({
            "status": "response_received", "http_status": response.status_code,
            "operation_id": response.headers.get("x-ms-operation-id"),
            "location": response.headers.get("Location"),
            "response_headers": dict(response.headers),
        })
        self.save()
        try:
            body = response.json() if response.content else {}
        except ValueError as error:
            action["response_body_error"] = "invalid-json"
            action["response_body_text"] = response.text
            self.save()
            raise PrototypePublicationError("Invalid Fabric JSON response; operation evidence retained") from error
        action["response_body"] = body
        if isinstance(body, dict):
            action["returned_item_id"] = body.get("id")
        elif body is not None or response.status_code != 202:
            action["response_body_error"] = "non-object"
        self.save()
        return self.checked(response)

    def _operation(self, action: dict[str, Any]) -> dict[str, Any]:
        operation_id = action.get("operation_id")
        location = action.get("location")
        if operation_id:
            url = f"{API}/operations/{uuid.UUID(operation_id)}"
        elif location and re.fullmatch(
            re.escape(API) + r"/operations/[0-9a-fA-F-]+/?", location
        ):
            url = location.rstrip("/")
        else:
            raise PrototypePublicationError("Ambiguous LRO: no durable Fabric operation reference")
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            response = self.request("GET", url)
            body = self.checked(response)
            action["operation_state"] = body.get("status")
            self.save()
            if body.get("status") == "Succeeded":
                return self.checked(self.request("GET", url + "/result"))
            if body.get("status") in ("Failed", "Cancelled"):
                action["status"] = "failed-retained"
                self.save()
                raise PrototypePublicationError(f"Fabric operation {body['status']}: {url}")
            try:
                delay = max(1, min(30, int(response.headers.get("Retry-After", "5"))))
            except ValueError:
                delay = 5
            time.sleep(delay)
        raise PrototypePublicationError(f"LRO still pending; resume this journal: {url}")

    def create(self, kind: str, definition: dict[str, Any] | None = None) -> str:
        actions = self.data["actions"]
        key = f"create:{kind}"
        request = {
            "displayName": self.plan["names"][kind],
            "description": self.plan["description"],
        }
        if kind == "lakehouse":
            request["creationPayload"] = {"enableSchemas": True}
        else:
            request["definition"] = definition
        request_hash = canonical_sha256(request)
        action = actions.get(key)
        if action is None:
            collisions = [
                item for item in self.items()
                if item.get("displayName", "").casefold() == request["displayName"].casefold()
            ]
            if collisions:
                raise PrototypePublicationError(f"Existing-name collision: {request['displayName']}")
            action = {
                "status": "intent", "kind": kind, "request_hash": request_hash,
                "display_name": request["displayName"],
            }
            actions[key] = action
            self.save()
            response = self.request(
                "POST", f"{API}/workspaces/{self.plan['workspace_id']}/{COLLECTIONS[kind]}",
                json=request,
            )
            body = self._receive(response, action)
        else:
            if action.get("request_hash") != request_hash:
                raise PrototypePublicationError(f"Create request changed: {key}")
            if action.get("ownership") == "operator-reconciled":
                from fabric_kg_builder.deploy.schema2_prototype_reconcile import validate_reconciled_create

                return validate_reconciled_create(self, kind, definition)
            body = {}
            if action["status"] == "intent":
                raise PrototypePublicationError(
                    f"Ambiguous prior create {key}; reconcile, never blind-retry"
                )
            if action.get("http_status") not in (200, 201, 202):
                raise PrototypePublicationError(f"Prior create rejected; retained journal: {key}")
        item_id = action.get("item_id") or action.get("returned_item_id")
        if action.get("http_status") == 202 and action.get("operation_state") != "Succeeded":
            body = self._operation(action)
            item_id = body.get("id") or item_id
            if item_id:
                action["returned_item_id"] = item_id
                self.save()
        elif not item_id and action.get("http_status") == 202:
            body = self._operation(action)
            item_id = body.get("id")
        if not item_id:
            raise PrototypePublicationError(f"Create result has no item ID: {key}; no name adoption")
        item_id = str(uuid.UUID(item_id))
        action.update({"item_id": item_id, "status": "created"})
        self.save()
        actual = self.checked(self.request(
            "GET", f"{API}/workspaces/{self.plan['workspace_id']}/{COLLECTIONS[kind]}/{item_id}"
        ))
        if (
            actual.get("id") != item_id
            or actual.get("displayName") != request["displayName"]
            or actual.get("type") != ITEM_TYPES[kind]
        ):
            raise PrototypePublicationError(f"Created item identity/readback mismatch: {key}")
        action["metadata"] = actual
        action["status"] = "identity-verified"
        self.save()
        return item_id

    def delta(self, table_id: str, table: Any, lakehouse_id: str) -> None:
        from deltalake import CommitProperties, DeltaTable, write_deltalake

        key = f"delta:{table_id}"
        path = (
            f"abfss://{self.plan['workspace_id']}@onelake.dfs.fabric.microsoft.com"
            f"/{lakehouse_id}/{onelake_tables_path('dbo', table_id)}"
        )
        options = {
            "bearer_token": self.credential.get_token("https://storage.azure.com/.default").token,
            "use_fabric_endpoint": "true",
        }
        action = self.data["actions"].get(key)
        proof = _table_proof(table)
        if action is None:
            action = {"status": "intent", "path": path, "table_proof": proof}
            self.data["actions"][key] = action
            self.save()
            write_deltalake(
                path, table, mode="error", storage_options=options,
                commit_properties=CommitProperties(
                    max_commit_retries=0,
                    custom_metadata={
                        "prototype_run_id": self.plan["run_id"],
                        "prototype_plan_hash": self.plan["plan_hash"],
                        "prototype_action": key,
                    },
                ),
            )
            action["status"] = "write-returned"
            self.save()
        elif action.get("path") != path or action.get("table_proof") != proof:
            raise PrototypePublicationError(f"Delta intent changed: {table_id}")
        # Even an interrupted intent is resumed only by proving its original commit.
        # No failed/ambiguous write is issued again.
        delta = DeltaTable(path, storage_options=options)
        history = delta.history(1)
        if (
            delta.version() != 0 or not history
            or history[0].get("prototype_run_id") != self.plan["run_id"]
            or history[0].get("prototype_plan_hash") != self.plan["plan_hash"]
            or history[0].get("prototype_action") != key
        ):
            raise PrototypePublicationError(f"Delta commit ownership/version mismatch: {table_id}")
        actual = delta.to_pyarrow_table()
        if _table_proof(actual) != proof:
            raise PrototypePublicationError(f"Delta schema/row/value readback mismatch: {table_id}")
        action.update({"status": "verified", "delta_version": 0, "readback": proof})
        self.save()

    def definition(self, kind: str, item_id: str) -> dict[str, Any]:
        action: dict[str, Any] = {"kind": kind, "item_id": item_id, "status": "read-intent"}
        self.data["read_operations"].append(action)
        self.save()
        response = self.request(
            "POST",
            f"{API}/workspaces/{self.plan['workspace_id']}/{COLLECTIONS[kind]}/{item_id}/getDefinition",
        )
        body = self._receive(response, action)
        if response.status_code == 202:
            body = self._operation(action)
        action["status"] = "read-complete"
        self.save()
        definition = body.get("definition", body)
        if not isinstance(definition, dict) or not isinstance(definition.get("parts"), list):
            raise PrototypePublicationError(f"Missing definition readback for {kind}")
        return definition

    def observe_new_items(self) -> None:
        baseline = set(self.data.get("baseline_item_ids", []))
        primary = {
            action.get("item_id"): action.get("ownership", "journal-returned-id")
            for action in self.data["actions"].values()
            if action.get("kind") in COLLECTIONS
        }
        self.data["observed_new_items"] = [
            {
                "metadata": item,
                "ownership": primary[item["id"]] if item.get("id") in primary
                else "unattributed-new-item-or-service-companion-retained",
            }
            for item in self.items() if item.get("id") not in baseline
        ]
        self.save()

    def graph_counts(self, graph_id: str, expected: dict[str, int]) -> None:
        results: dict[str, Any] = {}
        for name, pattern in (("nodes", "(n)"), ("edges", "()-[e]->()")):
            query = f"MATCH {pattern} RETURN count(*) AS observed_count"
            body = self.checked(self.request(
                "POST", f"{API}/workspaces/{self.plan['workspace_id']}/graphModels/"
                f"{graph_id}/executeQuery?beta=true", json={"query": query},
            ))
            results[name] = body
            self.data.setdefault("graph_count_readbacks", {})[graph_id] = {
                "expected": expected, "results": results,
            }
            self.save()
            status = body.get("status", {}).get("code", "")
            rows = body.get("result", {}).get("data")
            if (
                not isinstance(status, str) or not status.startswith(("00", "01", "02", "03"))
                or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or body.get("result", {}).get("kind") != "TABLE"
                or set(rows[0]) != {"observed_count"} or type(rows[0].get("observed_count")) is not int
                or rows[0].get("observed_count") != expected[name]
                or body.get("continuationToken") or body.get("continuationUri")
                or body.get("result", {}).get("continuationToken") or body.get("result", {}).get("continuationUri")
            ):
                raise PrototypePublicationError(
                    f"Graph {name} query/readback not ready or mismatched; "
                    "retain resources and resume, do not recreate/guess refresh jobs"
                )

    def graph_content(
        self, graph_id: str, definition: dict[str, Any], compilation: _Compilation,
        lakehouse_id: str, *, companion: bool = False,
        native_ontology: dict[str, Any] | None = None,
    ) -> None:
        checks = _graph_readback_checks(
            definition, compilation, self.plan["workspace_id"], lakehouse_id,
            companion=companion, native_ontology=native_ontology,
        )
        evidence = self.data.setdefault("graph_content_readbacks", {})
        evidence[graph_id] = {"status": "verifying", "checks": []}
        self.save()
        policy = self.plan["graph_readback_policy"]
        expected_total = sum(len(check.rows) for check in checks)
        if expected_total > policy["max_total_rows"]:
            raise PrototypePublicationError("Complete graph exceeds approved total readback cap")
        for check in checks:
            for query, expected_rows in check.windows(policy["page_size"]):
                record = {"query": query, "status": "unverified", "pages": []}
                evidence[graph_id]["checks"].append(record)
                self.save()
                rows: list[dict[str, Any]] = []
                continuation = None
                seen = set()
                for _ in range(policy["max_pages_per_window"]):
                    requests = self.data.get("graph_readback_request_count", 0) + 1
                    if requests > policy["max_requests"]:
                        raise PrototypePublicationError("Approved readback request cap exhausted")
                    self.data["graph_readback_request_count"] = requests
                    url = (
                        f"{API}/workspaces/{self.plan['workspace_id']}/graphModels/"
                        f"{graph_id}/executeQuery?beta=true"
                    )
                    if continuation:
                        url += "&continuationToken=" + quote(continuation, safe="")
                    body = self.checked(self.request("POST", url, json={"query": query}))
                    result = body.get("result", {})
                    page_rows = result.get("data")
                    status = body.get("status", {}).get("code", "")
                    if (
                        not isinstance(status, str) or not status.startswith(("00", "01", "02", "03"))
                        or result.get("kind") != "TABLE" or not isinstance(page_rows, list)
                        or result.get("continuationToken") or result.get("continuationUri")
                        or body.get("continuationUri") and not body.get("continuationToken")
                    ):
                        record["error_response"] = body
                        self.save()
                        raise PrototypePublicationError("Graph scalar/topology readback failed or has unsupported framing")
                    rows.extend(page_rows)
                    read_rows = self.data.get("graph_readback_row_count", 0) + len(page_rows)
                    self.data["graph_readback_row_count"] = read_rows
                    record["pages"].append({
                        "row_count": len(page_rows), "response_hash": canonical_sha256(body),
                        "status": status,
                    })
                    self.save()
                    if len(rows) > len(expected_rows) or read_rows > policy["max_total_rows"]:
                        raise PrototypePublicationError("Readback found extra rows or exceeded the approved total cap")
                    token = body.get("continuationToken")
                    if token is not None and not isinstance(token, str):
                        raise PrototypePublicationError("Invalid readback continuation token")
                    if not token:
                        continuation = None
                        break
                    if token in seen or len(token) > 8192:
                        raise PrototypePublicationError("Repeated/invalid readback continuation token")
                    seen.add(token)
                    continuation = token
                if continuation:
                    raise PrototypePublicationError("Readback page budget exhausted; complete set not verified")
                expected = _graph_row_fingerprint(expected_rows, check.fields, from_wire=False)
                observed = _graph_row_fingerprint(rows, check.fields, from_wire=True)
                record.update({
                    "expected_hash": expected, "observed_hash": observed,
                    "expected_rows": len(expected_rows), "observed_rows": len(rows),
                })
                self.save()
                if expected != observed:
                    record["mismatch_sample"] = rows[:10]
                    self.save()
                    raise PrototypePublicationError(
                        "Graph scalar values or actual endpoint-pair multiset differ from the complete compiled key window"
                    )
                record["status"] = "verified"
                self.save()
        evidence[graph_id]["status"] = "verified"
        self.save()

    def companion_readiness(
        self, *, ontology_id: str, lakehouse_id: str, expected: dict[str, int],
        bound_tables: set[str], compilation: _Compilation,
    ) -> bool:
        """Names select read candidates only; actual source bindings prove linkage."""
        self.observe_new_items()
        candidates = [
            item["metadata"] for item in self.data["observed_new_items"]
            if item["ownership"] not in ("journal-returned-id", "operator-reconciled")
            and ontology_id.replace("-", "") in item["metadata"].get("displayName", "")
        ]
        self.data["service_companion_candidates"] = candidates
        self.save()
        verified: list[str] = []
        for item in candidates:
            if item.get("type") != "GraphModel":
                continue
            graph_id = str(uuid.UUID(item["id"]))
            try:
                native = self.definition("graph", graph_id)
                payloads = _definition_payloads(native)
                _validate_companion_parts(native, item)
                observed_tables = set(_graph_source_tables(
                    payloads["dataSources.json"], self.plan["workspace_id"], lakehouse_id,
                    bound_tables, companion=True,
                ).values())
                if observed_tables != bound_tables:
                    raise PrototypePublicationError("Companion omits required Ontology source tables")
                self.data.setdefault("verified_service_companions", {})[graph_id] = {
                    "ontology_id": ontology_id,
                    "lakehouse_id": lakehouse_id,
                    "source_tables": sorted(observed_tables),
                    "definition_readback_hash": canonical_sha256(payloads),
                    "evidence": "new-after-baseline; ontology-id naming; exact actual source bindings",
                    "readiness": "source-linkage-only",
                }
                self.save()
                self.graph_counts(graph_id, expected)
                self.graph_content(
                    graph_id, native, compilation, lakehouse_id, companion=True,
                    native_ontology=self.definition("ontology", ontology_id),
                )
                self.data["verified_service_companions"][graph_id]["readiness"] = (
                    "scalars-and-endpoint-pairs-verified"
                )
                self.save()
                verified.append(graph_id)
            except Exception as error:
                self.data.setdefault("companion_readback_errors", {})[graph_id] = {
                    "type": type(error).__name__,
                    "detail": str(error) if isinstance(error, PrototypePublicationError)
                    else "Companion readback unavailable; no refresh or mutation attempted",
                }
                self.save()
        return len(verified) == 1


_GRAPH_SCHEMA_ROOT = "https://developer.microsoft.com/json-schemas/fabric/item/"
_INSTANCE_SOURCES_SCHEMA = _GRAPH_SCHEMA_ROOT + "graphInstance/definition/dataSources/1.0.0/schema.json"


def _graph_source_tables(
    sources: dict[str, Any], workspace_id: str, lakehouse_id: str,
    allowed_tables: Any, *, companion: bool,
) -> dict[str, str]:
    """Resolve exact, versioned source bindings; never rewrite native evidence."""
    instance = sources.get("$schema") == _INSTANCE_SOURCES_SCHEMA
    if instance:
        if not companion or set(sources) != {"$schema", "dataSources"}:
            raise PrototypePublicationError("Unsupported companion source dialect")
        references = {}
    else:
        if sources.get("$schema") != _GRAPH_SCHEMA_ROOT + "graphIndex/definition/dataSources/1.1.0/schema.json":
            raise PrototypePublicationError("Unsupported Graph data source schema")
        items = sources.get("itemReferences", [])
        references = {item["name"]: item["item"] for item in items}
        if len(references) != len(items):
            raise PrototypePublicationError("Duplicate Graph item references")
    result = {}
    for source in sources.get("dataSources", []):
        properties = source["properties"]
        path = properties.get("path")
        if not isinstance(path, str) or source.get("type") != "DeltaTable":
            raise PrototypePublicationError("Graph source must be an exact DeltaTable binding")
        if instance:
            match = re.fullmatch(
                r"abfss://([0-9a-f-]+)@onelake\.pbidedicated\.windows\.net/"
                r"([0-9a-f-]+)/Tables/dbo/([A-Za-z_][A-Za-z0-9_]*)", path,
            )
            if (
                set(source) != {"name", "type", "properties"} or set(properties) != {"path"}
                or match is None
            ):
                raise PrototypePublicationError("Unsafe or unsupported native companion source path")
            source_workspace, source_lakehouse, table_id = match.groups()
            if any(str(uuid.UUID(value)) != value for value in (source_workspace, source_lakehouse)):
                raise PrototypePublicationError("Companion source GUID is not canonical")
        else:
            reference = references.get(properties.get("referenceName"), {})
            source_workspace, source_lakehouse = reference.get("workspaceId"), reference.get("itemId")
            table_id = path.removeprefix("Tables/dbo/")
            if path != onelake_tables_path("dbo", table_id):
                raise PrototypePublicationError("Unsupported relative Graph source path")
        name = source.get("name")
        if (
            source_workspace != workspace_id or source_lakehouse != lakehouse_id
            or table_id not in allowed_tables or not isinstance(name, str) or not name
            or name in result or table_id in result.values()
        ):
            raise PrototypePublicationError("Graph source identity/coverage is outside approved compiled tables")
        result[name] = table_id
    if not result:
        raise PrototypePublicationError("Graph source bindings absent")
    return result


def _validate_companion_parts(
    definition: dict[str, Any], metadata: dict[str, Any] | None = None,
) -> None:
    payloads = _definition_payloads(definition)
    if payloads.get("dataSources.json", {}).get("$schema") != _INSTANCE_SOURCES_SCHEMA:
        return
    if set(payloads) != {
        "dataSources.json", "graphType.json", "graphDefinition.json",
        "stylingConfiguration.json", "graphSettings.json",
    }:
        raise PrototypePublicationError("Unsupported native companion definition parts")
    for name in ("graphType", "graphDefinition", "stylingConfiguration"):
        if payloads[name + ".json"].get("$schema") != (
            _GRAPH_SCHEMA_ROOT + f"graphInstance/definition/{name}/1.0.0/schema.json"
        ):
            raise PrototypePublicationError("Mixed or unsupported companion part schema")
    graph_type, bindings = payloads["graphType.json"], payloads["graphDefinition.json"]
    if set(graph_type) != {"$schema", "nodeTypes", "edgeTypes"} or set(bindings) != {
        "$schema", "nodeTables", "edgeTables",
    }:
        raise PrototypePublicationError("Unsupported companion graph type/definition fields")
    for kind, keys in (
        ("nodeTypes", {"alias", "labels", "primaryKeyProperties", "properties"}),
        ("edgeTypes", {"alias", "labels", "sourceNodeType", "destinationNodeType", "properties"}),
    ):
        for item in graph_type[kind]:
            if (
                set(item) != keys or not isinstance(item.get("alias"), str) or not item["alias"]
                or any(set(prop) != {"name", "type"} for prop in item["properties"])
            ):
                raise PrototypePublicationError("Unsupported companion type/property declaration")
            if kind == "edgeTypes" and any(
                set(item[key]) != {"alias"} for key in ("sourceNodeType", "destinationNodeType")
            ):
                raise PrototypePublicationError("Unsupported companion endpoint declaration")
    binding_ids = set()
    for kind, keys in (
        ("nodeTables", {"id", "nodeTypeAlias", "dataSourceName", "propertyMappings"}),
        ("edgeTables", {
            "id", "edgeTypeAlias", "dataSourceName", "propertyMappings",
            "sourceNodeKeyColumns", "destinationNodeKeyColumns", "edgeIdMapping",
        }),
    ):
        for item in bindings[kind]:
            if (
                set(item) != keys or item.get("edgeIdMapping") is not None
                or str(uuid.UUID(item["id"])) != item["id"] or item["id"] in binding_ids
                or any(set(mapping) != {"propertyName", "sourceColumn"} for mapping in item["propertyMappings"])
            ):
                raise PrototypePublicationError("Unsupported companion table/key/property mapping")
            binding_ids.add(item["id"])
    if payloads["graphSettings.json"] != {
        "$schema": _GRAPH_SCHEMA_ROOT + "graphIndex/definition/graphSettings/1.0.0/schema.json",
    }:
        raise PrototypePublicationError("Unsupported companion graph settings")
    styling = payloads["stylingConfiguration.json"]
    layout = styling.get("modelLayout", {})
    if (
        set(styling) != {"$schema", "modelLayout", "visualFormat", "scenario"}
        or styling["scenario"] != "Ontology" or styling["visualFormat"] is not None
        or styling["modelLayout"] != {
            "positions": {}, "styles": {}, "pan": {"x": 0, "y": 0}, "zoomLevel": 1,
        }
        or any(type(value) not in (int, float) for value in (
            layout.get("pan", {}).get("x"), layout.get("pan", {}).get("y"), layout.get("zoomLevel"),
        ))
    ):
        raise PrototypePublicationError("Unsupported companion styling/scenario")
    platforms = [part for part in definition["parts"] if part.get("path") == ".platform"]
    if len(platforms) != 1 or platforms[0].get("payloadType") != "InlineBase64":
        raise PrototypePublicationError("Missing or duplicate companion platform metadata")
    envelope = json.loads(base64.b64decode(platforms[0]["payload"], validate=True).decode("utf-8-sig"))
    if (
        set(envelope) != {"$schema", "metadata", "config"}
        or envelope["$schema"] != "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"
        or set(envelope["metadata"]) != {"type", "displayName"}
        or envelope["metadata"]["type"] != "GraphModel"
        or not isinstance(envelope["metadata"]["displayName"], str)
        or not envelope["metadata"]["displayName"]
        or set(envelope["config"]) != {"version", "logicalId"}
        or envelope["config"]["version"] != "2.0"
        or str(uuid.UUID(envelope["config"]["logicalId"])) != envelope["config"]["logicalId"]
        or metadata is not None and any(
            envelope["metadata"][key] != metadata.get(key) for key in ("type", "displayName")
        )
    ):
        raise PrototypePublicationError("Companion platform metadata/schema/config mismatch")


def _quote_graph_identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", identifier):
        raise PrototypePublicationError(f"Unsupported graph schema identifier: {identifier!r}")
    return f"`{identifier}`"


def _graph_row_fingerprint(
    rows: list[dict[str, Any]], fields: list[Any], *, from_wire: bool,
) -> str:
    import pyarrow as pa

    expected_keys = {f"c{index}" for index in range(len(fields))}
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise PrototypePublicationError("Graph returned missing or unexpected scalar columns")
        values = {}
        for index, field in enumerate(fields):
            value = row[f"c{index}"]
            if from_wire and isinstance(value, str) and pa.types.is_timestamp(field.type):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            values[f"c{index}"] = _typed_fingerprint_scalar(field.type, value)
        normalized.append(canonical_json(values))
    return canonical_sha256(sorted(normalized))


def _graph_readback_checks(
    definition: dict[str, Any], compilation: _Compilation,
    workspace_id: str, lakehouse_id: str, *, companion: bool,
    native_ontology: dict[str, Any] | None = None,
) -> list[_GraphReadbackCheck]:
    import pyarrow as pa

    payloads = _definition_payloads(definition)
    sources = payloads["dataSources.json"]
    if companion:
        _validate_companion_parts(definition)
    tables_by_source = _graph_source_tables(
        sources, workspace_id, lakehouse_id, compilation.tables, companion=companion,
    )
    ontology = compilation.definitions["ontology"]
    types_by_id = {
        item["canonical_semantic_type_id"]: item for item in ontology["entity_types"]
    }
    required_columns = {
        item["physical_table_id"]: {
            item["physical_identity_column"], "__label",
            *(prop["physical_column_id"] for prop in item["properties"]),
        }
        for item in ontology["entity_types"]
    }
    identity_columns = {
        item["physical_table_id"]: item["physical_identity_column"]
        for item in ontology["entity_types"]
    }
    widened = companion and any(
        len(item["allowed_source_semantic_type_ids"]) > 1
        or len(item["allowed_target_semantic_type_ids"]) > 1
        for item in ontology["relationship_types"]
    )
    if widened:
        required_columns[instance_base_entity_table(ontology)] = {"entity_id", "label"}
        identity_columns[instance_base_entity_table(ontology)] = "entity_id"
    graph_type = payloads["graphType.json"]
    graph_definition = payloads["graphDefinition.json"]
    ontology_node_bindings: dict[str, tuple[str, dict[str, str]]] = {}
    ontology_edge_labels: dict[str, str] = {}
    if companion:
        ontology_payloads = _definition_payloads(
            native_ontology if native_ontology is not None else {"parts": _ontology_parts(
                compilation, workspace_id, lakehouse_id, "schema2_readback", "Readback compilation"
            )}
        )
        for path, part in ontology_payloads.items():
            if "/DataBindings/" in path:
                entity = ontology_payloads[f"EntityTypes/{path.split('/')[1]}/definition.json"]
                names_by_id = {prop["id"]: prop["name"] for prop in entity["properties"]}
                configuration = part["dataBindingConfiguration"]
                ontology_node_bindings[configuration["sourceTableProperties"]["sourceTableName"]] = (
                    entity["name"],
                    {
                        names_by_id[binding["targetPropertyId"]]: binding["sourceColumnName"]
                        for binding in configuration["propertyBindings"]
                    },
                )
            elif "/Contextualizations/" in path:
                relationship = ontology_payloads[
                    f"RelationshipTypes/{path.split('/')[1]}/definition.json"
                ]
                ontology_edge_labels[part["dataBindingTable"]["sourceTableName"]] = relationship["name"]
    node_types = {item["alias"]: item for item in graph_type["nodeTypes"]}
    edge_types = {item["alias"]: item for item in graph_type["edgeTypes"]}
    if (
        len(node_types) != len(graph_type["nodeTypes"]) or len(edge_types) != len(graph_type["edgeTypes"])
        or set(node_types) != {item["nodeTypeAlias"] for item in graph_definition["nodeTables"]}
        or set(edge_types) != {item["edgeTypeAlias"] for item in graph_definition["edgeTables"]}
        or set(tables_by_source) != {
            item["dataSourceName"] for item in graph_definition["nodeTables"] + graph_definition["edgeTables"]
        }
    ):
        raise PrototypePublicationError("Duplicate, unused or missing native Graph types/sources")
    nodes: dict[str, tuple[str, str, str]] = {}
    observed_node_tables: list[str] = []
    observed_edges: list[tuple[str, str, str]] = []
    checks = []

    def source_table(binding: dict[str, Any]) -> tuple[str, Any]:
        if binding.get("filter"):
            raise PrototypePublicationError("Filtered native graph readback is not implemented; readiness unverified")
        table_id = tables_by_source[binding["dataSourceName"]]
        table = compilation.tables[table_id]
        return table_id, table

    def mappings(binding: dict[str, Any], table: Any) -> dict[str, str]:
        items = binding["propertyMappings"]
        mapped = {item["propertyName"]: item["sourceColumn"] for item in items}
        if len(mapped) != len(items) or not set(mapped.values()).issubset(table.column_names):
            raise PrototypePublicationError("Invalid/duplicate graph scalar mappings")
        for property_name in mapped:
            _quote_graph_identifier(property_name)
        return mapped

    def check_property_types(native_type: dict[str, Any], mapped: dict[str, str], table: Any) -> None:
        properties = native_type.get("properties", [])
        declared = {prop["name"]: prop["type"] for prop in properties}
        if len(declared) != len(properties) or set(declared) != set(mapped):
            raise PrototypePublicationError("Graph scalar schema and bindings disagree")
        for name, column in mapped.items():
            arrow_type = table.schema.field(column).type
            expected_type = (
                "STRING" if pa.types.is_string(arrow_type)
                else "BOOLEAN" if pa.types.is_boolean(arrow_type)
                else "INT" if pa.types.is_integer(arrow_type)
                else "DOUBLE" if pa.types.is_floating(arrow_type)
                else "ZONED DATETIME" if pa.types.is_timestamp(arrow_type)
                else None
            )
            if expected_type is None or declared[name] != expected_type:
                raise PrototypePublicationError(
                    f"Graph stored scalar type is unsupported or differs from compiled schema: {column}"
                )

    def add_check(
        pattern: str, table: Any, selected: list[tuple[str, str]],
        key_bindings: list[tuple[str, str]],
    ) -> None:
        fields = [table.schema.field(column) for _, column in selected]
        expected_rows = [
            {f"c{index}": row[column] for index, (_, column) in enumerate(selected)}
            for row in table.to_pylist()
        ]
        returns = ", ".join(
            f"{expression} AS c{index}" for index, (expression, _) in enumerate(selected)
        )
        columns = [column for _, column in selected]
        keys = [(expression, f"c{columns.index(column)}") for expression, column in key_bindings]
        checks.append(_GraphReadbackCheck(pattern, returns, expected_rows, fields, keys))

    for binding in graph_definition["nodeTables"]:
        table_id, table = source_table(binding)
        alias = binding["nodeTypeAlias"]
        node = node_types[alias]
        mapped = mappings(binding, table)
        check_property_types(node, mapped, table)
        keys = node["primaryKeyProperties"]
        if (
            alias in nodes or table_id not in required_columns
            or not required_columns[table_id].issubset(mapped.values())
            or len(keys) != 1 or mapped.get(keys[0]) != identity_columns[table_id]
            or len(node["labels"]) != 1
        ):
            raise PrototypePublicationError("Graph omits/changes required semantic properties or canonical identity")
        label = _quote_graph_identifier(node["labels"][0])
        if companion and ontology_node_bindings.get(table_id) != (node["labels"][0], mapped):
            raise PrototypePublicationError(
                "Companion Graph property names/bindings differ from the published Ontology; readiness unverified"
            )
        nodes[alias] = (table_id, label, _quote_graph_identifier(keys[0]))
        observed_node_tables.append(table_id)
        add_check(
            f"(n:{label})", table,
            [(f"n.{_quote_graph_identifier(prop)}", column) for prop, column in mapped.items()],
            [(f"n.{_quote_graph_identifier(keys[0])}", identity_columns[table_id])],
        )
    if sorted(observed_node_tables) != sorted(required_columns):
        raise PrototypePublicationError("Graph semantic node coverage differs from compiled authority")
    for binding in graph_definition["edgeTables"]:
        table_id, table = source_table(binding)
        edge = edge_types[binding["edgeTypeAlias"]]
        source = nodes[edge["sourceNodeType"]["alias"]]
        target = nodes[edge["destinationNodeType"]["alias"]]
        source_keys = binding["sourceNodeKeyColumns"]
        target_keys = binding["destinationNodeKeyColumns"]
        if (
            source_keys != ["__source_entity_id"] or target_keys != ["__target_entity_id"]
            or len(edge["labels"]) != 1
        ):
            raise PrototypePublicationError("Graph edge does not bind canonical endpoint identities")
        mapped = mappings(binding, table)
        check_property_types(edge, mapped, table)
        if companion and ontology_edge_labels.get(table_id) != edge["labels"][0]:
            raise PrototypePublicationError(
                "Companion relationship label differs from the published Ontology; readiness unverified"
            )
        if not companion and not {
            "__canonical_id", "__semantic_relationship_id", "__source_entity_id", "__target_entity_id",
        }.issubset(mapped.values()):
            raise PrototypePublicationError("Independent Graph omits asserted edge identity/properties")
        observed_edges.append((table_id, source[0], target[0]))
        edge_identity = [
            (f"e.{_quote_graph_identifier(prop)}", column)
            for prop, column in mapped.items() if column == "__canonical_id"
        ]
        add_check(
            f"(s:{source[1]})-[e:{_quote_graph_identifier(edge['labels'][0])}]->(t:{target[1]})",
            table,
            [(f"s.{source[2]}", source_keys[0]), (f"t.{target[2]}", target_keys[0])]
            + [(f"e.{_quote_graph_identifier(prop)}", column) for prop, column in mapped.items()],
            edge_identity[:1] or [(f"s.{source[2]}", source_keys[0]), (f"t.{target[2]}", target_keys[0])],
        )
    if companion:
        def endpoint_table(allowed: list[str]) -> str:
            return (
                instance_base_entity_table(ontology) if len(allowed) > 1
                else types_by_id[allowed[0]]["physical_table_id"]
            )
        expected_edges = [
            (item["physical_table_id"],
             endpoint_table(item["allowed_source_semantic_type_ids"]),
             endpoint_table(item["allowed_target_semantic_type_ids"]))
            for item in ontology["relationship_types"]
        ]
    else:
        expected_edges = [
            (item["physical_table_id"],
             types_by_id[item["source_type"]]["physical_table_id"],
             types_by_id[item["target_type"]]["physical_table_id"])
            for item in compilation.graph_catalog["edges"]
        ]
    if sorted(observed_edges) != sorted(expected_edges):
        raise PrototypePublicationError("Graph endpoint type topology differs from approved compilation")
    return checks


def _definition_payloads(definition: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for part in definition["parts"]:
        if part["path"] == ".platform":
            continue
        if part["payloadType"] != "InlineBase64" or part["path"] in result:
            raise PrototypePublicationError("Unverifiable or duplicate native definition part")
        decoded = base64.b64decode(part["payload"], validate=True).decode("utf-8-sig")
        if part["path"].endswith(".json") or part["path"] == "definition.pbism":
            result[part["path"]] = json.loads(decoded)
        else:
            result[part["path"]] = decoded.replace("\r\n", "\n")
    return result


def _native_definition_payloads_equal(
    kind: str, expected: dict[str, Any], actual: dict[str, Any],
) -> bool:
    """Compare full payloads, allowing only observed serialization defaults.

    Keep raw definitions/hashes as evidence. Never normalize source mappings,
    graph types, key order, unknown fields, or nonempty service settings.
    """
    def normalized(payloads: dict[str, Any]) -> dict[str, Any]:
        if kind == "semantic_model":
            return {
                path: value.rstrip("\n")
                if path.endswith(".tmdl") and isinstance(value, str) else value
                for path, value in payloads.items()
            }
        if kind != "graph":
            return payloads
        result = copy.deepcopy(payloads)
        schema_root = "https://developer.microsoft.com/json-schemas/fabric/item/graphIndex/definition"
        if result.get("graphSettings.json") == {
            "$schema": f"{schema_root}/graphSettings/1.0.0/schema.json",
        }:
            del result["graphSettings.json"]
        graph = result.get("graphDefinition.json")
        if isinstance(graph, dict) and graph.get("$schema") == (
            f"{schema_root}/graphDefinition/1.0.0/schema.json"
        ):
            for edge in graph.get("edgeTables", []):
                if isinstance(edge, dict) and edge.get("edgeIdMapping") is None:
                    edge.pop("edgeIdMapping", None)
        styling = result.get("stylingConfiguration.json")
        if isinstance(styling, dict) and styling.get("$schema") == (
            f"{schema_root}/stylingConfiguration/1.0.0/schema.json"
        ):
            if styling.get("visualFormat") is None:
                styling.pop("visualFormat", None)
            layout = styling.get("modelLayout")
            if isinstance(layout, dict):
                numeric_fields = [(layout, "zoomLevel")]
                positions = layout.get("positions", {})
                points = list(positions.values()) if isinstance(positions, dict) else []
                points.append(layout.get("pan"))
                for point in points:
                    if isinstance(point, dict):
                        numeric_fields.extend((point, axis) for axis in ("x", "y"))
                for container, key in numeric_fields:
                    value = container.get(key)
                    if type(value) is float and value.is_integer():
                        container[key] = int(value)
        return result

    return canonical_sha256(normalized(expected)) == canonical_sha256(normalized(actual))


def _validate_companion_key_windows(compilation: _Compilation, page_size: int) -> None:
    # Ontology companion edges need endpoint-pair paging, not independent-graph edge IDs.
    columns = ["__source_entity_id", "__target_entity_id"]
    for relationship in compilation.definitions["ontology"]["relationship_types"]:
        table_id = relationship["physical_table_id"]
        check = _GraphReadbackCheck(
            "(s)-[e]->(t)", "s, t",
            compilation.tables[table_id].select(columns).to_pylist(), [],
            [("s.id", columns[0]), ("t.id", columns[1])],
        )
        try:
            for _query, _rows in check.windows(page_size):
                pass
        except PrototypePublicationError as error:
            raise PrototypePublicationError(
                f"Ontology companion readback for {table_id}: {error}"
            ) from error


def publish_schema2_prototype(
    *, l4_run: Path, l3_root: Path, workspace_id: str, name_prefix: str,
    dry_run: bool, approve_live: str | None, plan_path: Path,
    materialize_dir: Path | None, journal_path: Path | None,
    approved_limitations: tuple[str, ...] = (), semantic_model: bool = False,
    readback_page_size: int = MAX_GRAPH_READBACK_ROWS,
    readback_total_rows: int = DEFAULT_GRAPH_READBACK_TOTAL_ROWS,
    quality_policy: dict[str, Any] | None = None,
    _candidate_only: bool = False,
) -> dict[str, Any]:
    """Plan, or execute exactly that plan, without invoking transactional L5a."""
    workspace_id = str(uuid.UUID(workspace_id))
    if (
        type(readback_page_size) is not int or not 1 <= readback_page_size <= MAX_GRAPH_READBACK_ROWS
        or type(readback_total_rows) is not int or not 1 <= readback_total_rows <= 10_000_000
    ):
        raise PrototypePublicationError("Invalid prototype readback page size/total cap")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", name_prefix):
        raise PrototypePublicationError(
            "Prototype prefix must start with a letter and contain at most 48 letters/digits/underscores"
        )
    if materialize_dir is None or journal_path is None:
        raise PrototypePublicationError("Prototype requires explicit --materialize and --prototype-journal")
    if len({plan_path.resolve(), journal_path.resolve()}) != 2:
        raise PrototypePublicationError("Plan and journal paths must be distinct")
    from contextlib import nullcontext

    if not _candidate_only:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock_context = (
        nullcontext(None) if _candidate_only
        else journal_path.with_suffix(journal_path.suffix + ".lock").open("a")
    )
    with lock_context as lock:
        try:
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PrototypePublicationError("This prototype journal is already active") from None
        previous = _read_json(plan_path) if plan_path.exists() else None
        if previous is not None and previous.get("plan_hash") != canonical_sha256({
            key: value for key, value in previous.items() if key != "plan_hash"
        }):
            raise PrototypePublicationError("Immutable plan hash mismatch")
        if not dry_run and previous is None:
            raise PrototypePublicationError("Live execution requires an existing immutable dry-run plan")
        if previous is not None and quality_policy is not None and "business_quality" not in previous:
            raise PrototypePublicationError("Quality opt-in requires a new plan, not a historical repair")
        if previous is not None and "business_quality" in previous:
            bound_policy = previous["business_quality"]["policy"]
            if quality_policy is not None:
                from fabric_kg_builder.serving.business_quality import parse_quality_policy

                if parse_quality_policy(quality_policy).model_dump(mode="json") != bound_policy:
                    raise PrototypePublicationError("Immutable business quality policy changed")
            quality_policy = bound_policy
        returned_runtime_repair = False
        if not _candidate_only and journal_path.exists():
            from fabric_kg_builder.deploy.schema2_prototype_reconcile import (
                RETURNED_ID_POLICY, validate_returned_artifacts,
            )

            journal = _read_json(journal_path)
            validate_returned_artifacts(journal, materialize_dir)
            returned_runtime_repair = journal.get("runtime_repair", {}).get("policy") == RETURNED_ID_POLICY
        run_id = previous["run_id"] if previous else uuid.uuid4().hex
        kinds = ["lakehouse", "ontology", "graph"]
        if semantic_model:
            kinds.append("semantic_model")
        names = {kind: f"{name_prefix}_{run_id[:12]}_{kind}" for kind in kinds}
        compilation = _compile(
            l4_run, l3_root, workspace_id, name_prefix,
            **({"quality_policy": quality_policy} if quality_policy is not None else {}),
        )
        quality_report = compilation.definitions["ontology"].get("business_quality")
        if quality_policy is not None and quality_report is None:
            raise PrototypePublicationError("Quality-enabled publication lacks a recomputed quality report")
        if previous is not None and "business_quality" in previous:
            if quality_report != previous["business_quality"]:
                raise PrototypePublicationError("Immutable business quality assessment changed; create a new plan")
        description = _prototype_description(
            run_id, compilation.provenance.get("window_run_scope"),
            compilation.provenance.get("partial_extraction_scope"),
        )
        ontology = compilation.definitions["ontology"]
        widened = any(
            len(item["allowed_source_semantic_type_ids"]) > 1
            or len(item["allowed_target_semantic_type_ids"]) > 1
            for item in ontology["relationship_types"]
        )
        companion_rows = sum(
            compilation.tables[item["physical_table_id"]].num_rows
            for item in ontology["entity_types"] + ontology["relationship_types"]
        ) + (compilation.tables[instance_base_entity_table(ontology)].num_rows if widened else 0)
        expected_readback_rows = (
            compilation.graph_catalog["expected_node_count"]
            + compilation.graph_catalog["expected_edge_count"] + companion_rows
        )
        if expected_readback_rows > readback_total_rows:
            compilation.blockers.append(
                f"Complete readback needs {expected_readback_rows} rows; approved total cap is {readback_total_rows}"
            )
        if not _candidate_only and not returned_runtime_repair:
            _materialize(compilation, materialize_dir)
        native: dict[str, Any] = {}
        try:
            native, exclusions = _native_definitions(
                compilation, workspace_id=workspace_id,
                lakehouse_id=LAKEHOUSE_REFERENCE, names=names,
                description=description, root=materialize_dir,
                semantic_model=semantic_model,
            )
            compilation.limitations.extend(exclusions)
            checks = _graph_readback_checks(
                native["graph"], compilation, workspace_id, LAKEHOUSE_REFERENCE,
                companion=False,
            )
            for check in checks:
                for _query, _rows in check.windows(readback_page_size):
                    pass
            _validate_companion_key_windows(compilation, readback_page_size)
        except ValueError as error:
            compilation.blockers.append(str(error))
        limitations = sorted(set(compilation.limitations))
        approvals = sorted(set(approved_limitations))
        unknown = sorted(set(approvals) - set(limitations))
        if unknown:
            raise PrototypePublicationError(f"Unknown limitation approvals: {unknown}")
        blockers = sorted(set(compilation.blockers) | {
            f"unapproved:{item}" for item in set(limitations) - set(approvals)
        })
        plan = {
            "plan_version": FORMAT_VERSION, "mode": "prototype-create-only",
            "run_id": run_id, "workspace_id": workspace_id, "name_prefix": name_prefix,
            "names": names, "description": description,
            "policy": "create-only-retain-partial-no-delete-no-overwrite-no-adoption",
            "semantic_model": semantic_model,
            "compiler_hash": _compiler_hash(),
            "provenance": compilation.provenance,
            "tables": {
                table_id: _table_proof(table)
                for table_id, table in sorted(compilation.tables.items())
            },
            "graph_catalog": compilation.graph_catalog,
            "native_definition_templates": native,
            "late_binding": {LAKEHOUSE_REFERENCE: "journal-returned-created-lakehouse-id"},
            "dependency_order": ["create:lakehouse", "create-only-delta-and-readback"]
            + [f"create:{kind}" for kind in kinds if kind != "lakehouse"],
            "limitations": limitations, "approved_limitations": approvals,
            "blockers": blockers,
            "framing_policy": "observe-auto-framing-only; unsupported-or-pending-remains-partial",
            "graph_readback_policy": {
                "page_size": readback_page_size,
                "overflow_probe_rows_per_window": 1,
                "max_total_rows": readback_total_rows,
                "expected_total_rows": expected_readback_rows,
                "max_pages_per_window": readback_page_size + 1,
                "max_requests": max(1000, min(100_000, 4 * (
                    (readback_total_rows + readback_page_size - 1) // readback_page_size
                ) + 200)),
                "budget_scope": "one-cli-invocation-across-all-graphs",
                "pagination": "immutable-canonical-key-windows-and-validated-service-cursors",
                "verification": "all-mapped-scalars-and-actual-canonical-endpoint-pair-multisets",
            },
            "semantic_model_readiness_policy": (
                "definition-readback-only; separate-live-model-query-verification-required"
                if semantic_model else "not-requested"
            ),
            "business_acceptance": "requires-six-deployed-source-cited-question-results",
        }
        if quality_report is not None:
            plan["business_quality"] = quality_report
        plan["plan_hash"] = canonical_sha256(plan)
        if _candidate_only:
            return plan
        if previous is not None and previous != plan:
            from fabric_kg_builder.deploy.schema2_prototype_reconcile import validate_runtime_repair

            if not journal_path.exists():
                raise PrototypePublicationError(
                    "Immutable plan changed; an existing journal and accepted operator runtime-repair review are required"
                )
            validate_runtime_repair(
                previous, plan, _read_json(journal_path),
                plan_bytes=plan_path.read_bytes(),
            )
            plan = previous
        elif previous is not None and journal_path.exists():
            journal = _read_json(journal_path)
            if journal.get("runtime_repair"):
                from fabric_kg_builder.deploy.schema2_prototype_reconcile import validate_runtime_repair

                validate_runtime_repair(previous, plan, journal, plan_bytes=plan_path.read_bytes())
        if previous is None:
            _atomic_json(plan_path, plan, create=True)
        for kind, definition in native.items():
            _atomic_json(
                materialize_dir / "native-templates" / f"{kind}.json",
                definition, create=True,
            )
        if dry_run:
            return plan
        if approve_live != plan["plan_hash"]:
            raise PrototypePublicationError("Live prototype requires the exact --approve-live plan hash")
        if blockers:
            raise PrototypePublicationError(f"Prototype plan blocked: {blockers}")
        from fabric_kg_builder.deploy.schema2_prototype_reconcile import validate_returned_resume

        validate_returned_resume(journal_path, plan, materialize_dir, l4_run, l3_root)
        run = _Run(journal_path, plan)
        if "graph_readback_row_count" in run.data:
            run.data.setdefault("prior_readback_attempts", []).append({
                "row_count": run.data["graph_readback_row_count"],
                "request_count": run.data.get("graph_readback_request_count", 0),
            })
        run.data["graph_readback_row_count"] = 0
        run.data["graph_readback_request_count"] = 0
        run.data["status"] = "publication-or-readback-in-progress"
        run.data["ontology_readiness"] = "unverified-this-invocation"
        run.data["semantic_model_readiness"] = (
            "unverified-this-invocation" if semantic_model else "not-requested"
        )
        run.save()
        try:
            if "baseline_item_ids" not in run.data:
                baseline = run.items()
                conflicts = [
                    item["displayName"] for item in baseline
                    if item.get("displayName", "").casefold()
                    in {name.casefold() for name in names.values()}
                ]
                if conflicts:
                    raise PrototypePublicationError(f"Existing-name collisions: {conflicts}")
                run.data["baseline_item_ids"] = [item["id"] for item in baseline]
                run.data["baseline_graph_count"] = sum(
                    item.get("type") == "GraphModel" for item in baseline
                )
                run.save()
            if run.data["baseline_graph_count"] + 2 > 10:
                raise PrototypePublicationError(
                    "Insufficient GraphModel slots (independent graph plus Ontology companion needs two)"
                )
            lakehouse_id = run.create("lakehouse")
            lakehouse = run.data["actions"]["create:lakehouse"]["metadata"]
            if resolve_lakehouse_schema(lakehouse) != "dbo":
                raise PrototypePublicationError("Created Lakehouse did not confirm defaultSchema=dbo")
            for table_id, table in sorted(compilation.tables.items()):
                run.delta(table_id, table, lakehouse_id)
            definitions, exclusions = _native_definitions(
                compilation, workspace_id=workspace_id, lakehouse_id=lakehouse_id,
                names=names, description=description, root=materialize_dir,
                semantic_model=semantic_model,
            )
            if sorted(exclusions) != sorted(
                item for item in limitations if item.startswith("semantic-model.")
            ):
                raise PrototypePublicationError("Semantic Model exclusions changed after approval")
            ids = {"lakehouse": lakehouse_id}
            for kind in kinds[1:]:
                definition = definitions[kind]
                _atomic_json(
                    materialize_dir / "native-bound" / f"{kind}.json",
                    definition, create=True,
                )
                ids[kind] = run.create(kind, definition)
                run.observe_new_items()
                actual = run.definition(kind, ids[kind])
                _atomic_json(
                    materialize_dir / "native-readback" / f"{kind}.{canonical_sha256(actual)}.json",
                    actual, create=True,
                )
                expected_payloads = _definition_payloads(definition)
                actual_payloads = _definition_payloads(actual)
                if not _native_definition_payloads_equal(kind, expected_payloads, actual_payloads):
                    raise PrototypePublicationError(f"Native {kind} definition readback drift")
                run.data["actions"][f"create:{kind}"]["definition_readback_hash"] = (
                    canonical_sha256(actual_payloads)
                )
                if kind == "semantic_model":
                    run.data["semantic_model_readiness"] = "definition-verified-query-unverified"
                run.save()
            run.graph_counts(ids["graph"], {
                "nodes": compilation.graph_catalog["expected_node_count"],
                "edges": compilation.graph_catalog["expected_edge_count"],
            })
            run.graph_content(
                ids["graph"], definitions["graph"], compilation, lakehouse_id
            )
            ontology = compilation.definitions["ontology"]
            bound_tables = {
                item["physical_table_id"]
                for item in ontology["entity_types"] + ontology["relationship_types"]
            }
            widened = any(
                item.startswith("ontology.endpoint-widening:") for item in limitations
            )
            if widened:
                bound_tables.add(instance_base_entity_table(ontology))
            companion_ready = run.companion_readiness(
                ontology_id=ids["ontology"], lakehouse_id=lakehouse_id,
                bound_tables=bound_tables, compilation=compilation,
                expected={
                    "nodes": compilation.graph_catalog["expected_node_count"]
                    + (compilation.tables[instance_base_entity_table(ontology)].num_rows if widened else 0),
                    "edges": sum(
                        compilation.tables[item["physical_table_id"]].num_rows
                        for item in ontology["relationship_types"]
                    ),
                },
            )
            run.data.update({
                "status": "structural-verification-complete"
                if companion_ready and not semantic_model else "partial",
                "structural_publication": "definitions-delta-values-and-independent-graph-scalars-topology-verified",
                "ontology_readiness": "companion-bindings-scalars-and-endpoint-pairs-verified"
                if companion_ready else "companion-framing-and-query-not-yet-verified",
                "semantic_model_readiness": "definition-verified-query-unverified"
                if semantic_model else "not-requested",
                "item_ids": ids,
            })
            run.save()
            return {"plan_hash": plan["plan_hash"], "journal": str(journal_path), **run.data}
        except Exception as error:
            run.data["status"] = "partial-retained"
            run.data["last_error"] = {
                "type": type(error).__name__,
                "message": str(error) if isinstance(error, PrototypePublicationError)
                else "External operation failed; inspect durable action state before reconciliation",
            }
            run.save()
            try:
                run.observe_new_items()
            except Exception:
                pass
            raise PrototypePublicationError(
                f"Prototype stopped; all partial resources retained. Journal: {journal_path}. "
                + run.data["last_error"]["message"]
            ) from error
