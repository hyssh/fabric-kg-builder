"""Offline, non-authoritative review of a Graphify nodes/links export."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from fabric_kg_builder.contracts.base import RequiredText, Sha256, canonical_sha256
from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import (
    SealedL4HierarchyRows,
    build_hierarchy_core_rows,
)

UPSTREAM_SHA = "59b5ab1151e031fb42e39cfc9a50bf96f2d57ae0"
GRAPHIFY_VERSION = "0.9.102"
MAX_REVIEW_INPUT_BYTES = 16 * 1024 * 1024


class _Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EntityBinding(_Binding):
    graphify_id: RequiredText
    entity_id: RequiredText
    semantic_type_id: RequiredText
    evidence_span_ids: list[RequiredText]


class RelationshipBinding(_Binding):
    link_index: Annotated[int, Field(ge=0)]
    relationship_id: RequiredText
    semantic_relationship_id: RequiredText
    evidence_span_ids: list[RequiredText]


class GraphifyBindings(_Binding):
    """Project-owned associations, not an upstream Graphify contract."""

    schema_version: Literal["1.0"]
    upstream_sha: Literal["59b5ab1151e031fb42e39cfc9a50bf96f2d57ae0"]
    graphify_version: Literal["0.9.102"]
    artifact_sha256: Sha256
    sealed_manifest_hash: Sha256
    entities: list[EntityBinding]
    relationships: list[RelationshipBinding]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def read_review_json(path: Path) -> tuple[object, str]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_REVIEW_INPUT_BYTES + 1)
    _require(
        len(raw) <= MAX_REVIEW_INPUT_BYTES,
        f"review input exceeds {MAX_REVIEW_INPUT_BYTES} byte limit: {path}",
    )
    try:
        value = json.loads(
            raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
    except RecursionError as exc:
        raise ValueError(f"review JSON is too deeply nested: {path}") from exc
    return value, hashlib.sha256(raw).hexdigest()


def _invalid_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON value is not allowed: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite JSON value is not allowed: {value}")
    return number


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _rows(graph: dict, key: str) -> list[dict]:
    rows = graph.get(key)
    _require(isinstance(rows, list), f"Graphify export requires {key!r} array")
    _require(all(isinstance(row, dict) for row in rows), f"{key} must contain objects")
    return rows


def _evidence(actual: list[str], expected: list[str] | None, label: str) -> None:
    _require(
        bool(actual) and len(actual) == len(set(actual)),
        f"{label}: evidence must be nonempty and unique",
    )
    _require(
        expected is not None and sorted(actual) == sorted(expected),
        f"{label}: evidence bindings differ from sealed L4",
    )


def review_graphify_import(
    *,
    sealed: SealedL4HierarchyRows,
    graph_path: Path,
    bindings_path: Path,
) -> dict[str, object]:
    """Check exact associations; never produce facts or a publishable hierarchy.

    The sidecar is a caller's explicit association of external rows with sealed
    facts. Matching evidence IDs does not verify Graphify's own source content,
    extraction quality, community semantics, or the claimed producer binary.
    """
    graph, artifact_hash = read_review_json(graph_path)
    raw_bindings, bindings_hash = read_review_json(bindings_path)
    bindings = GraphifyBindings.model_validate(raw_bindings)
    _require(isinstance(graph, dict), "Graphify export must be an object")
    _require(bindings.artifact_sha256 == artifact_hash, "artifact SHA256 mismatch")
    _require(
        bindings.sealed_manifest_hash == sealed.manifest.manifest_hash,
        "sealed L4 manifest hash mismatch",
    )
    _require(graph.get("directed") is True, "directed Graphify export required")
    _require(type(graph.get("multigraph")) is bool, "Graphify multigraph flag required")
    _require("edges" not in graph, "Graphify export uses links, not edges")
    metadata = graph.get("graph")
    _require(isinstance(metadata, dict), "Graphify graph metadata required")
    _require(
        type(metadata.get("schema_version")) is int
        and metadata["schema_version"] == 1,
        "Graphify graph.schema_version must be integer 1",
    )
    exported_version = metadata.get("graphify_version")
    _require(
        exported_version is None or exported_version == GRAPHIFY_VERSION,
        "exported Graphify version conflicts with bindings",
    )
    _require(
        "hyperedges" not in graph or graph["hyperedges"] == [],
        "hyperedges are not supported by this review",
    )
    nodes, links = _rows(graph, "nodes"), _rows(graph, "links")
    core = build_hierarchy_core_rows(sealed)
    entities = {row.entity_id: row for row in core.entities}
    relationships = {row.relationship_id: row for row in core.relationships}
    _require(
        len(entities) == len(core.entities)
        and len(relationships) == len(core.relationships),
        "sealed graph identities must be unique",
    )
    _require(bool(entities), "empty sealed graph cannot establish an import review")

    node_by_id: dict[str, dict] = {}
    for node in nodes:
        node_id = node.get("id")
        _require(isinstance(node_id, str) and bool(node_id.strip()), "invalid node id")
        _require(node_id not in node_by_id, f"duplicate node id: {node_id}")
        node_by_id[node_id] = node
    by_external = {row.graphify_id: row for row in bindings.entities}
    canonical_ids = [row.entity_id for row in bindings.entities]
    _require(
        len(by_external) == len(bindings.entities)
        and len(set(canonical_ids)) == len(canonical_ids),
        "entity bindings must be a bijection",
    )
    _require(set(by_external) == set(node_by_id), "node binding coverage mismatch")
    _require(set(canonical_ids) == set(entities), "sealed entity coverage mismatch")
    communities: dict[int, list[str]] = {}
    for external_id, binding in by_external.items():
        entity = entities[binding.entity_id]
        _require(
            binding.semantic_type_id == entity.entity_type,
            f"{external_id}: entity type binding mismatch",
        )
        _evidence(binding.evidence_span_ids, entity.evidence_ids, external_id)
        community = node_by_id[external_id].get("community")
        _require(
            type(community) is int and community >= 0,
            f"{external_id}: nonnegative integer community required; no inferred assignment",
        )
        communities.setdefault(community, []).append(binding.entity_id)

    by_index = {row.link_index: row for row in bindings.relationships}
    relationship_ids = [row.relationship_id for row in bindings.relationships]
    _require(
        len(by_index) == len(bindings.relationships)
        and len(set(relationship_ids)) == len(relationship_ids),
        "relationship bindings must be a bijection",
    )
    _require(set(by_index) == set(range(len(links))), "link binding coverage mismatch")
    _require(
        set(relationship_ids) == set(relationships), "sealed relationship coverage mismatch"
    )
    for index, link in enumerate(links):
        source, target = link.get("source"), link.get("target")
        _require(
            isinstance(source, str) and source in by_external
            and isinstance(target, str) and target in by_external,
            f"link {index}: unknown or invalid endpoint",
        )
        binding = by_index[index]
        relationship = relationships[binding.relationship_id]
        _require(
            by_external[source].entity_id == relationship.source_entity_id
            and by_external[target].entity_id == relationship.target_entity_id,
            f"link {index}: directed endpoints differ from sealed L4",
        )
        _require(
            binding.semantic_relationship_id == relationship.relationship_type
            and link.get("relation") == relationship.relationship_type,
            f"link {index}: relationship type differs from sealed L4",
        )
        _evidence(
            binding.evidence_span_ids, relationship.evidence_ids, f"link {index}"
        )
    report: dict[str, object] = {
        "schema_version": "1.0",
        "kind": "graphify_import_review",
        "status": "shadow_review_only",
        "publishable": False,
        "upstream_sha_claim": bindings.upstream_sha,
        "graphify_version_claim": bindings.graphify_version,
        "producer_provenance_verified": False,
        "artifact_sha256": artifact_hash,
        "bindings_sha256": bindings_hash,
        "sealed_manifest_hash": sealed.manifest.manifest_hash,
        "sealed_receipt_hash": sealed.receipt.receipt_hash,
        "input_graph_hash": core.input_graph_hash,
        "partial_scope": sealed.partial_scope,
        "entity_count": len(entities),
        "relationship_count": len(relationships),
        "flat_communities": [
            {"external_community": key, "entity_ids": sorted(communities[key])}
            for key in sorted(communities)
        ],
        "limitations": [
            "Caller-declared identity/type/evidence associations only; not extraction validation.",
            "Graphify file_type/confidence/source_file are not L3 evidence or domain authority.",
            "No image-only evidence promotion; no new evidence spans are minted.",
            "Flat community labels are not a hierarchy, semantic inheritance, or quality proof.",
            "Not a derived-hierarchy artifact or publication receipt; cannot authorize Fabric writes.",
        ],
    }
    report["review_hash"] = canonical_sha256(report)
    return report
