"""Project derived Leiden communities onto native Ontology entity inheritance.

Fabric Ontology entity types may declare a single ``baseEntityTypeId``. This
module turns the frontier of the derived community hierarchy into abstract
(binding-less) community entity types and makes every extracted entity type
inherit from the community holding the plurality of its instances::

    surface_entity (1000000, abstract root)
      └── group_<dominant type> (5000001+, abstract community)
            └── <extracted entity type> (bound to its Lakehouse table)

Live-probed Fabric constraints this module encodes:

* a derived type must *redefine* the inherited key/label properties
  (``redefines`` + ``baseTypeNamespaceType: "Custom"``); pure inheritance of the
  base key fails the import;
* inheritance is single-parent and acyclic, and data bindings and relationship
  types are not inherited, so only one community level is projected;
* abstract types carry no key (``entityIdParts == []``) and no data binding.

The function is a pure post-processing step over already compiled and
presentation-repaired parts, so output with the feature off is unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from fabric_kg_builder.deploy.fabric_ontology_definition import (
    BASE_ENTITY_TYPE_ID,
    BASE_IDENTITY_PROPERTY_ID,
    BASE_LABEL_PROPERTY_ID,
    _NAMESPACE,
    _SCHEMA_ROOT,
    _part,
)
from fabric_kg_builder.deploy.ontology_names import NATIVE_NAME_PATTERN

INHERITANCE_MODE = "leiden_community"
COMMUNITY_TYPE_BIGINT_BASE = 5_000_000
COMMUNITY_TYPE_CAPACITY = 1_000_000
COMMUNITY_KEY_PROPERTY_NAME = "community_key"
_NAME_PREFIX = "group_"


class OntologyInheritanceError(ValueError):
    """Raised when community inheritance cannot be projected safely."""


def _decode(part: Mapping[str, str]) -> dict[str, Any]:
    return json.loads(base64.b64decode(part["payload"]))


def _entity_type_id(path: str) -> str | None:
    match = re.fullmatch(r"EntityTypes/(\d+)/definition\.json", path)
    return match.group(1) if match else None


def _rows(table: Any) -> list[dict[str, Any]]:
    return table.to_pylist() if hasattr(table, "to_pylist") else list(table)


def _select_frontier(communities: Sequence[Mapping[str, Any]]) -> list[str]:
    """Roots, descending through a single all-covering root when possible."""

    children: dict[str | None, list[str]] = defaultdict(list)
    for row in communities:
        children[row.get("parent_community_id")].append(str(row["community_id"]))
    frontier = sorted(children.get(None, []))
    while len(frontier) == 1 and children.get(frontier[0]):
        frontier = sorted(children[frontier[0]])
    return frontier


def _unique_name(base: str, taken: set[str], key: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", base)
    if not name or not name[0].isalpha():
        name = "g_" + name
    name = name[:128]
    if name.casefold() not in taken:
        return name
    suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    name = f"{name[:117]}_{suffix}"
    counter = 1
    candidate = name
    while candidate.casefold() in taken:
        candidate = f"{name[:113]}_{counter}"
        counter += 1
    return candidate


def apply_community_inheritance(
    parts: Sequence[dict[str, str]],
    *,
    l5a_ontology: Mapping[str, Any],
    community_tables: Mapping[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Return inheritance-projected parts and a deterministic mapping report.

    ``parts`` must contain the base entity type definition. Its data binding is
    left as given; callers decide whether the root stays bound (endpoint
    widening) or becomes abstract.
    """

    if "graph_communities" not in community_tables or "graph_community_members" not in community_tables:
        raise OntologyInheritanceError(
            "Ontology inheritance requires derived hierarchy tables; pass --hierarchy-state"
        )
    communities = _rows(community_tables["graph_communities"])
    members = _rows(community_tables["graph_community_members"])
    numeric_by_canonical = {
        str(item["canonical_semantic_type_id"]): str(item["id"])
        for item in l5a_ontology["entity_types"]
    }

    decoded: dict[str, dict[str, Any]] = {}
    for part in parts:
        type_id = _entity_type_id(part["path"])
        if type_id is not None:
            decoded[type_id] = _decode(part)
    if BASE_ENTITY_TYPE_ID not in decoded:
        raise OntologyInheritanceError("Ontology inheritance requires the base entity type part")
    base = decoded[BASE_ENTITY_TYPE_ID]
    base_props = {prop["id"]: prop for prop in base["properties"]}
    if BASE_IDENTITY_PROPERTY_ID not in base_props:
        raise OntologyInheritanceError("Base entity type lacks its identity property")
    derived_ids = sorted(set(numeric_by_canonical.values()))
    if set(decoded) - {BASE_ENTITY_TYPE_ID} != set(derived_ids):
        raise OntologyInheritanceError("Native entity types differ from the L5a ontology")
    if any(decoded[type_id].get("baseEntityTypeId") is not None for type_id in decoded):
        raise OntologyInheritanceError("Compiled entity types already declare inheritance")

    frontier = _select_frontier(communities)
    frontier_set = set(frontier)
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in members:
        community_id = str(row["community_id"])
        if community_id in frontier_set:
            counts[str(row["entity_type"])][community_id] += 1
    unknown = sorted(set(counts) - set(numeric_by_canonical))
    if unknown:
        raise OntologyInheritanceError(f"Community members reference unpublished types: {unknown[:5]}")

    assignment: dict[str, str] = {}
    for canonical, counter in counts.items():
        best = max(counter.values())
        assignment[canonical] = min(cid for cid, n in counter.items() if n == best)

    used = sorted({cid for cid in assignment.values()})
    if len(used) > COMMUNITY_TYPE_CAPACITY - 1:
        raise OntologyInheritanceError("Community type id capacity exceeded")
    type_names = {type_id: payload["name"] for type_id, payload in decoded.items()}
    taken = {name.casefold() for name in type_names.values()}
    for part in parts:
        if part["path"].startswith("RelationshipTypes/") and part["path"].endswith("/definition.json"):
            taken.add(_decode(part)["name"].casefold())

    canonical_by_numeric = {numeric: canonical for canonical, numeric in numeric_by_canonical.items()}
    community_types: dict[str, dict[str, Any]] = {}
    community_parts: list[dict[str, str]] = []
    report_communities: list[dict[str, Any]] = []
    for index, community_id in enumerate(used, start=1):
        type_id = str(COMMUNITY_TYPE_BIGINT_BASE + index)
        member_types = sorted(
            (canonical for canonical, cid in assignment.items() if cid == community_id),
            key=lambda canonical: (-counts[canonical][community_id], canonical),
        )
        dominant_name = type_names[numeric_by_canonical[member_types[0]]]
        name = _unique_name(_NAME_PREFIX + dominant_name, taken, community_id)
        taken.add(name.casefold())
        member_names = [type_names[numeric_by_canonical[c]] for c in member_types]
        description = (
            f"Leiden community {community_id} grouping: " + ", ".join(member_names)
        )
        if len(description) > 512:
            description = description[:509] + "..."
        payload = {
            "$schema": f"{_SCHEMA_ROOT}/entityType/1.0.0/schema.json",
            "id": type_id,
            "namespace": _NAMESPACE,
            "baseEntityTypeId": BASE_ENTITY_TYPE_ID,
            "name": name,
            "entityIdParts": [],
            "displayNamePropertyId": None,
            "namespaceType": "Custom",
            "visibility": "Visible",
            "properties": [{
                "id": f"7{type_id}",
                "name": COMMUNITY_KEY_PROPERTY_NAME,
                "redefines": None,
                "baseTypeNamespaceType": None,
                "valueType": "String",
            }],
            "timeseriesProperties": [],
            "semanticEnrichment": {
                "synonyms": [],
                "description": description,
                "customAttributes": {},
            },
            "untypedProperties": [],
        }
        if not re.fullmatch(NATIVE_NAME_PATTERN, name):
            raise OntologyInheritanceError(f"Invalid community type name {name!r}")
        community_types[community_id] = payload
        community_parts.append(_part(f"EntityTypes/{type_id}/definition.json", payload))
        report_communities.append({
            "community_id": community_id,
            "entity_type_id": type_id,
            "name": name,
            "member_entity_types": member_names,
        })

    report_types: list[dict[str, Any]] = []
    rewritten: dict[str, dict[str, str]] = {}
    for type_id in derived_ids:
        payload = decoded[type_id]
        canonical = canonical_by_numeric[type_id]
        community_id = assignment.get(canonical)
        parent = (
            community_types[community_id]["id"] if community_id is not None else BASE_ENTITY_TYPE_ID
        )
        identity_id = f"9{type_id}"
        label_id = f"8{type_id}"
        redefined = {identity_id: BASE_IDENTITY_PROPERTY_ID}
        if BASE_LABEL_PROPERTY_ID in base_props:
            redefined[label_id] = BASE_LABEL_PROPERTY_ID
        inherited_names = {base_props[pid]["name"].casefold(): pid for pid in redefined.values()}
        props = []
        seen: set[str] = set()
        for prop in payload["properties"]:
            if prop["id"] in redefined:
                base_prop = base_props[redefined[prop["id"]]]
                prop = {
                    **prop,
                    "name": base_prop["name"],
                    "redefines": base_prop["id"],
                    "baseTypeNamespaceType": "Custom",
                    "valueType": base_prop["valueType"],
                }
                seen.add(prop["id"])
            elif prop["name"].casefold() in inherited_names:
                raise OntologyInheritanceError(
                    f"Entity type {type_id} property {prop['name']!r} shadows an inherited property"
                )
            props.append(prop)
        if identity_id not in seen:
            raise OntologyInheritanceError(
                f"Entity type {type_id} lacks a redefinable identity property"
            )
        new_payload = {**payload, "baseEntityTypeId": parent, "properties": props}
        rewritten[f"EntityTypes/{type_id}/definition.json"] = _part(
            f"EntityTypes/{type_id}/definition.json", new_payload
        )
        report_types.append({
            "entity_type_id": type_id,
            "canonical_semantic_type_id": canonical,
            "name": payload["name"],
            "base_entity_type_id": parent,
            "community_id": community_id,
            "frontier_member_count": counts[canonical][community_id] if community_id else 0,
        })

    result: list[dict[str, str]] = []
    inserted = False
    for part in parts:
        if not inserted and (
            part["path"].startswith("RelationshipTypes/") or part["path"] == ".platform"
        ):
            result.extend(community_parts)
            inserted = True
        result.append(rewritten.get(part["path"], part))
    if not inserted:
        result.extend(community_parts)

    report = {
        "mode": INHERITANCE_MODE,
        "frontier_community_ids": frontier,
        "community_types": report_communities,
        "entity_types": report_types,
        "unassigned_entity_type_count": sum(1 for item in report_types if item["community_id"] is None),
    }
    _validate_tree(result)
    return result, report


def _validate_tree(parts: Sequence[Mapping[str, str]]) -> None:
    parents: dict[str, str | None] = {}
    for part in parts:
        type_id = _entity_type_id(part["path"])
        if type_id is not None:
            parents[type_id] = _decode(part).get("baseEntityTypeId")
    for type_id in parents:
        seen = {type_id}
        current = parents[type_id]
        while current is not None:
            if current not in parents:
                raise OntologyInheritanceError(f"Entity type {type_id} inherits from unknown {current}")
            if current in seen:
                raise OntologyInheritanceError(f"Inheritance cycle at {type_id}")
            seen.add(current)
            current = parents[current]
