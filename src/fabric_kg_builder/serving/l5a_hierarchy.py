"""Fail-closed reader and physical projection of sealed derived hierarchies."""

from __future__ import annotations

import hashlib
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.contracts.derived_hierarchy_receipt import DerivedHierarchyReceipt
from fabric_kg_builder.contracts.receipts import ArtifactManifest
from fabric_kg_builder.graph.community_contracts import (
    DerivedClusterRow,
    DerivedHierarchyOutcome,
    DerivedMembershipRow,
    HierarchyStatus,
)
from fabric_kg_builder.graph.community_identity import (
    compute_evidence_binding_hash,
    compute_execution_fingerprint,
)
from fabric_kg_builder.graph.community_provider import NativeLeidenProvider
from fabric_kg_builder.graph.community_validation import validate_derived_hierarchy
from fabric_kg_builder.model.derived_hierarchy_schemas import (
    DERIVED_HIERARCHY_CONTRACT_KINDS,
    DERIVED_HIERARCHY_TABLE_SCHEMAS,
)
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource

if TYPE_CHECKING:
    from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import SealedL4HierarchyCoreInput
    from fabric_kg_builder.serving.structured_publication import L5aPublicationError


def _source_core_input(source: SealedL4ServingSource) -> SealedL4HierarchyCoreInput:
    # One seam shared with the hierarchy CLI; never invent a second L4 conversion.
    from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import (
        build_hierarchy_core_rows,
        read_sealed_l4_hierarchy_rows,
    )

    return build_hierarchy_core_rows(read_sealed_l4_hierarchy_rows(source))


def _error(code: str, detail: str) -> L5aPublicationError:
    from fabric_kg_builder.serving.structured_publication import L5aPublicationError

    return L5aPublicationError(f"L5A_HIERARCHY_{code}", detail)


_COMMUNITY_COLUMNS = {
    "community_id": "Stable derived community identity; not a display label or native integer.",
    "level": "Leaf-first structural height: terminals are 0; parents are 1 + max child height.",
    "parent_community_id": "Actual parent community identity; null only at a root.",
    "size": "Number of distinct canonical entities in this community's descendant union.",
    "native_depth": "Native root-first partition depth; null for singleton isolates.",
    "is_terminal": "True for a terminal community with exactly one primary membership per entity.",
    "is_oversized": "Terminal exceeds the requested split trigger; not a hard-cap violation.",
    "run_id": "Derived hierarchy run identifier from the validated receipt.",
    "execution_fingerprint": "SHA-256 binding the original graph snapshot and hierarchy settings.",
    "input_graph_hash": "SHA-256 of the original canonical directed, typed graph snapshot.",
    "sealed_l4_manifest_hash": "Sealed L4 manifest hash whose canonical entities are published.",
    "hierarchy_receipt_hash": "Validated derived hierarchy receipt content hash.",
}
_MEMBER_COLUMNS = {
    "community_id": _COMMUNITY_COLUMNS["community_id"],
    "level": _COMMUNITY_COLUMNS["level"],
    "entity_id": "Canonical entity ID; joins directly to semantic_asserted_entities.entity_id.",
    "entity_type": "Asserted semantic type ID; one row per community/entity/asserted type.",
}
HIERARCHY_TABLE_DESCRIPTIONS = {
    "graph_communities": {
        "description": "Derived Leiden communities, retaining actual hierarchy and sealed-source lineage.",
        "columns": _COMMUNITY_COLUMNS,
    },
    "graph_community_members": {
        "description": "Canonical entity/type membership at every realized community level, including ancestors.",
        "columns": _MEMBER_COLUMNS,
    },
}


def load_hierarchy_tables(
    source: SealedL4ServingSource, root: Path,
) -> tuple[dict[str, pa.Table], dict[str, Any]]:
    """Validate the entire frozen artifact before projecting any publication row."""
    try:
        receipt = DerivedHierarchyReceipt.model_validate_json(
            (root / "derived-hierarchy-receipt.json").read_bytes()
        )
        manifest = ArtifactManifest.model_validate_json(
            (root / "derived-hierarchy-manifest.json").read_bytes()
        )
    except (OSError, ValidationError, ValueError) as exc:
        raise _error("INVALID_RECEIPT", str(exc)) from exc
    if (
        receipt.sealed_l4_manifest_id != source.manifest.artifact_manifest_id
        or receipt.sealed_l4_manifest_hash != source.manifest.manifest_hash
        or receipt.sealed_l4_receipt_id != source.receipt.stage_receipt_id
        or receipt.sealed_l4_receipt_hash != source.receipt.receipt_hash
    ):
        raise _error("SOURCE_MISMATCH", "hierarchy does not bind to this sealed L4 run")
    expected_identity = source.manifest.identity.model_dump(
        mode="json", exclude={"contract_kind", "parent_artifact_ids"},
    )
    expected_parents = set(source.manifest.identity.parent_artifact_ids) | {
        source.manifest.artifact_manifest_id,
    }
    if any(
        identity.model_dump(mode="json", exclude={"contract_kind", "parent_artifact_ids"})
        != expected_identity
        or set(identity.parent_artifact_ids) != expected_parents
        for identity in (receipt.identity, manifest.identity)
    ):
        raise _error("SOURCE_MISMATCH", "hierarchy identity authority differs from sealed L4")
    if receipt.status not in {"complete", "empty", "insufficient"}:
        raise _error("STATUS_NOT_PUBLISHABLE", f"hierarchy status is {receipt.status}")
    if (
        receipt.realized_provider_id != NativeLeidenProvider.provider_id
        or receipt.realized_provider_version != NativeLeidenProvider.provider_version
    ):
        raise _error("PROVIDER_MISMATCH", "only the pinned native Leiden artifact is publishable")
    try:
        core_input = _source_core_input(source)
    except ImportError as exc:
        raise _error(
            "SOURCE_CONVERSION_UNAVAILABLE",
            "the shared sealed-L4 hierarchy conversion helper is not installed",
        ) from exc
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise _error("SOURCE_INVALID", str(exc)) from exc
    if receipt.status == "empty" and (
        receipt.input_graph_hash is None or receipt.execution_fingerprint is None
    ):
        if core_input.entities or core_input.relationships:
            raise _error("STATUS_MISMATCH", "empty outcome differs from the sealed source graph")
        raise _error(
            "EMPTY_UNBOUND",
            "The source graph had no entities/edges; the EMPTY hierarchy has no "
            "input-graph/execution hash binding and cannot be published.",
        )
    if (
        receipt.output_manifest_id != manifest.artifact_manifest_id
        or receipt.output_manifest_hash != manifest.manifest_hash
        or receipt.receipt_contract_version != "1.0.0"
        or receipt.realized_hierarchy_version != "2.0-derived-leiden"
    ):
        raise _error("MANIFEST_MISMATCH", "receipt does not bind to the supported output manifest")
    expected_kinds = set(DERIVED_HIERARCHY_CONTRACT_KINDS.values())
    if (
        len(manifest.entries) != len(expected_kinds)
        or {entry.contract_kind for entry in manifest.entries} != expected_kinds
    ):
        raise _error("INCOMPLETE_ARTIFACT", "all three derived artifact tables are required")
    rows_by_name: dict[str, list[dict[str, Any]]] = {}
    for name, schema in DERIVED_HIERARCHY_TABLE_SCHEMAS.items():
        entry = next(
            item for item in manifest.entries
            if item.contract_kind == DERIVED_HIERARCHY_CONTRACT_KINDS[name]
        )
        path = root / f"{name.replace('_', '-')}.parquet"
        try:
            payload = path.read_bytes()
        except (OSError, pa.ArrowException) as exc:
            raise _error("INCOMPLETE_ARTIFACT", str(exc)) from exc
        if entry.content_hash != hashlib.sha256(payload).hexdigest():
            raise _error("TABLE_INTEGRITY", f"content mismatch: {name}")
        try:
            table = pq.read_table(pa.BufferReader(payload))
        except pa.ArrowException as exc:
            raise _error("TABLE_INTEGRITY", f"invalid Parquet: {name}") from exc
        schema_hash = canonical_sha256([
            {"name": field.name, "type": str(field.type), "nullable": field.nullable}
            for field in schema
        ])
        if (
            entry.contract_version != "1.0.0"
            or entry.schema_hash != schema_hash
            or entry.content_hash != hashlib.sha256(payload).hexdigest()
            or entry.byte_count != len(payload)
            or entry.row_count != table.num_rows
            # Parquet renames list children from "item" to "element".
            or not table.schema.equals(schema)
            or table.schema.metadata != schema.metadata
            or any(table[field.name].null_count for field in schema if not field.nullable)
        ):
            raise _error("TABLE_INTEGRITY", f"manifest/schema/content mismatch: {name}")
        rows = table.to_pylist()
        for row in rows:
            for field in schema:
                if pa.types.is_list(field.type) and (
                    any(not isinstance(value, str) or not value for value in row[field.name])
                    or len(row[field.name]) != len(set(row[field.name]))
                ):
                    raise _error("ROW_INTEGRITY", f"invalid canonical ID sequence: {field.name}")
            if row["row_hash"] != canonical_sha256({
                key: value for key, value in row.items() if key != "row_hash"
            }):
                raise _error("ROW_INTEGRITY", f"invalid row hash: {name}")
            for key, value in {
                "run_id": receipt.run_id, "config_id": receipt.config_id,
                "policy_id": receipt.policy_id, "seed": receipt.seed,
                "hierarchy_version": receipt.realized_hierarchy_version,
            }.items():
                if row[key] != value:
                    raise _error("ROW_LINEAGE", f"inconsistent {key}: {name}")
        rows_by_name[name] = rows
    expected_entities = {entity.entity_id for entity in core_input.entities}
    if core_input.input_graph_hash != receipt.input_graph_hash:
        raise _error("GRAPH_MISMATCH", "hierarchy input graph differs from the sealed L4 graph")
    cluster_fields = {field.name for field in fields(DerivedClusterRow)}
    clusters = [
        DerivedClusterRow(**{
            key: tuple(value) if isinstance(value, list) else value
            for key, value in row.items() if key in cluster_fields
        })
        for row in rows_by_name["derived_community_clusters"]
    ]
    memberships = [
        DerivedMembershipRow(
            cluster_id=row["cluster_id"], entity_id=row["entity_id"],
            evidence_ids=tuple(row["evidence_ids"]),
            primary_membership=row["primary_membership"],
        )
        for row in rows_by_name["derived_community_memberships"]
    ]
    domain_hashes = {entity.domain_hash for entity in core_input.entities}
    if len(domain_hashes) > 1 or any(
        cluster.domain_hash != next(iter(domain_hashes), None) for cluster in clusters
    ):
        raise _error("ROW_LINEAGE", "community domain authority must agree")
    if (
        receipt.max_cluster_size < 1 or not 0 <= receipt.seed < 2**64
        or receipt.realized_provider_id is None or receipt.realized_provider_version is None
    ):
        raise _error("EXECUTION_MISMATCH", "invalid hierarchy settings/provider identity")
    evidence_hash = compute_evidence_binding_hash(
        {entity.entity_id: tuple(entity.evidence_ids or ()) for entity in core_input.entities},
        {row.relationship_id: tuple(row.evidence_ids or ()) for row in core_input.relationships},
    )
    if receipt.evidence_binding_hash != evidence_hash:
        raise _error("EVIDENCE_MISMATCH", "evidence binding differs from sealed L4 evidence")
    expected_fingerprint = compute_execution_fingerprint(
        input_graph_hash=core_input.input_graph_hash, config_id=receipt.config_id,
        provider_id=receipt.realized_provider_id,
        provider_version=receipt.realized_provider_version,
        seed=receipt.seed, policy_id=receipt.policy_id,
        max_cluster_size=receipt.max_cluster_size,
        domain_hash=next(iter(domain_hashes), None),
        evidence_binding_hash=receipt.evidence_binding_hash,
    )
    if (
        expected_fingerprint != receipt.execution_fingerprint
        or any(
            cluster.execution_fingerprint != receipt.execution_fingerprint
            or cluster.provider_id != receipt.realized_provider_id
            or cluster.provider_version != receipt.realized_provider_version
            for cluster in clusters
        )
    ):
        raise _error("EXECUTION_MISMATCH", "hierarchy settings/fingerprint lineage differs")
    outcome = DerivedHierarchyOutcome(
        status=HierarchyStatus(receipt.status), clusters=clusters,
        memberships=memberships,
    )
    if {member.entity_id for member in memberships} != expected_entities:
        raise _error("INVALID_MEMBERSHIP", "terminal membership must conserve the sealed entity set")
    report = validate_derived_hierarchy(outcome)
    if not report.is_valid:
        raise _error("INVALID_MEMBERSHIP", "; ".join(
            f"{item.code}: {item.detail}" for item in report.findings
        ))
    if (receipt.status == "empty") != (not expected_entities):
        raise _error("STATUS_MISMATCH", "empty status must match the sealed entity set")
    has_edges = any(
        row.source_entity_id != row.target_entity_id for row in core_input.relationships
    )
    if (receipt.status == "insufficient") != (bool(expected_entities) and not has_edges):
        raise _error("STATUS_MISMATCH", "insufficient status must match an edgeless nonempty graph")
    from fabric_kg_builder.enrichment.approved_partial_handoff import read_scope

    if receipt.sealed_partial_scope_present != (
        read_scope(source.root, source.manifest) is not None
    ):
        raise _error("SOURCE_MISMATCH", "sealed partial scope differs")
    types_by_entity: dict[str, set[str]] = {entity: set() for entity in expected_entities}
    for row in pq.read_table(source.resolve("semantic_entity_type_assertions")).to_pylist():
        if row["entity_id"] not in types_by_entity:
            raise _error("ORPHAN_ENTITY", f"unknown type assertion entity: {row['entity_id']}")
        types_by_entity[row["entity_id"]].add(row["semantic_type_id"])
    if any(not types for types in types_by_entity.values()):
        raise _error("ENTITY_TYPE_MISSING", "every canonical entity must have an asserted type")
    community_rows = [
        {
            "community_id": cluster.cluster_id, "level": cluster.structural_height,
            "parent_community_id": cluster.parent_cluster_id,
            "size": len(cluster.member_entity_ids), "native_depth": cluster.native_depth,
            "is_terminal": cluster.is_terminal, "is_oversized": cluster.is_oversized,
            "run_id": receipt.run_id, "execution_fingerprint": receipt.execution_fingerprint,
            "input_graph_hash": receipt.input_graph_hash,
            "sealed_l4_manifest_hash": receipt.sealed_l4_manifest_hash,
            "hierarchy_receipt_hash": receipt.receipt_hash,
        }
        for cluster in sorted(clusters, key=lambda item: item.cluster_id)
    ]
    member_rows = [
        {
            "community_id": cluster.cluster_id, "level": cluster.structural_height,
            "entity_id": entity_id, "entity_type": entity_type,
        }
        for cluster in sorted(clusters, key=lambda item: item.cluster_id)
        for entity_id in sorted(cluster.member_entity_ids)
        for entity_type in sorted(types_by_entity[entity_id])
    ]
    community_schema = pa.schema([
        pa.field(name, (
            pa.int64() if name in {"level", "size", "native_depth"} else
            pa.bool_() if name in {"is_terminal", "is_oversized"} else pa.string()
        ), nullable=name in {"parent_community_id", "native_depth"},
            metadata={"description": description})
        for name, description in _COMMUNITY_COLUMNS.items()
    ], metadata={"description": HIERARCHY_TABLE_DESCRIPTIONS["graph_communities"]["description"]})
    member_schema = pa.schema([
        pa.field(name, pa.int64() if name == "level" else pa.string(),
                 nullable=False, metadata={"description": description})
        for name, description in _MEMBER_COLUMNS.items()
    ], metadata={"description": HIERARCHY_TABLE_DESCRIPTIONS["graph_community_members"]["description"]})
    binding = {
        "receipt_hash": receipt.receipt_hash, "manifest_hash": manifest.manifest_hash,
        "input_graph_hash": receipt.input_graph_hash,
        "execution_fingerprint": receipt.execution_fingerprint,
        "status": receipt.status,
        "table_crosswalk": HIERARCHY_TABLE_DESCRIPTIONS,
    }
    return {
        "graph_communities": pa.Table.from_pylist(community_rows, schema=community_schema),
        "graph_community_members": pa.Table.from_pylist(member_rows, schema=member_schema),
    }, binding
