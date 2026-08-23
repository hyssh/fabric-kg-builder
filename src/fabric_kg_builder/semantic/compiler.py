"""Deterministic shared compiler for every semantic serving projection."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from .artifact_validation import (
    ArtifactFinding,
    validate_crosswalk_against_manifest,
    validate_manifest_model_completeness,
    validate_materialization_availability,
)
from .enrichment import build_semantic_enrichment_context
from .models import EntityMapping, PropertyDefinition, RelationshipMapping
from .quality import EnrichmentQualityReport
from .query_validation import FABRIC_RESERVED_PHYSICAL_IDENTIFIERS
from .schemas import (
    AgentElementProjection,
    AgentPropertyChildProjection,
    ColumnSpec,
    DataAvailability,
    DependencyNode,
    EntityTableSpec,
    GraphEdgeProjection,
    GraphNodeProjection,
    GraphPropertyProjection,
    HierarchyMetadata,
    InversePolicy,
    ManifestEntityTypeEntry,
    ManifestPropertyEntry,
    ManifestRelationshipEntry,
    MaterializationPlan,
    ModelQualityFinding,
    ModelQualityMetrics,
    OntologyEntityProjection,
    OntologyPropertyProjection,
    OntologyRelationshipProjection,
    PublicationProfile,
    RelationshipTableSpec,
    SearchLinkageSpec,
    SemanticCrosswalk,
    SemanticDependencyGraph,
    SemanticModelManifest,
    SemanticModelQualityReport,
    TransitivityPolicy,
    CrosswalkEntry,
    compute_dependency_graph_hash,
    compute_manifest_hash,
    compute_model_quality_report_hash,
)
from .service import (
    SemanticBundle,
    normalize_semantic_contract,
    validate_semantic_bundle,
)
from .source_tables import resolve_semantic_source_parquet


class SemanticCompileError(ValueError):
    """Raised when an approved semantic bundle cannot be compiled safely."""


@dataclass(frozen=True)
class LoadedSemanticModelArtifacts:
    """Validated persisted authority consumed by projection-specific compilers."""

    manifest: SemanticModelManifest
    crosswalk: SemanticCrosswalk
    materialization_plan: MaterializationPlan
    quality_report: SemanticModelQualityReport
    dependency_graph: SemanticDependencyGraph


@dataclass(frozen=True)
class CompiledSemanticArtifacts:
    """Normalized outputs derived from one sealed semantic model manifest."""

    contract_hash: str
    normalized_contract: dict[str, Any]
    semantic_model_manifest: SemanticModelManifest
    semantic_crosswalk: SemanticCrosswalk
    materialization_plan: MaterializationPlan
    model_quality_report: SemanticModelQualityReport
    dependency_graph: SemanticDependencyGraph
    ontology_model: dict[str, Any]
    ontology_ids_lock: dict[str, Any]
    graph_entity_types: tuple[str, ...]
    graph_node_labels: dict[str, str]
    graph_relationships: tuple[dict[str, Any], ...]
    label_catalog: dict[str, Any]
    agent_semantic_context: dict[str, Any]

    def write(self, output_dir: Path | str) -> Path:
        """Write deterministic, reviewable compiler inputs."""
        out = Path(output_dir)
        (out / "ontology").mkdir(parents=True, exist_ok=True)
        (out / "graph").mkdir(parents=True, exist_ok=True)
        (out / "agents").mkdir(parents=True, exist_ok=True)
        _write_json(out / "normalized-contract.json", self.normalized_contract)
        _write_json(
            out / "semantic-model-manifest.json",
            self.semantic_model_manifest.model_dump(mode="json"),
        )
        _write_json(
            out / "semantic-crosswalk.json",
            self.semantic_crosswalk.model_dump(mode="json"),
        )
        _write_json(
            out / "materialization-plan.json",
            self.materialization_plan.model_dump(mode="json"),
        )
        _write_json(
            out / "model-quality-report.json",
            self.model_quality_report.model_dump(mode="json"),
        )
        _write_json(
            out / "dependency-graph.json",
            self.dependency_graph.model_dump(mode="json"),
        )
        (out / "ontology" / "model.yaml").write_text(
            yaml.safe_dump(
                {"ontology": self.ontology_model},
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        _write_json(
            out / "ontology" / "ids.lock.json",
            self.ontology_ids_lock,
        )
        _write_json(
            out / "graph" / "semantic-plan.json",
            {
                "schema_version": "1.1",
                "contract_hash": self.contract_hash,
                "semantic_model_manifest_hash": (
                    self.semantic_model_manifest.manifest_hash
                ),
                "entity_types": list(self.graph_entity_types),
                "node_labels": self.graph_node_labels,
                "relationships": list(self.graph_relationships),
            },
        )
        _write_json(out / "graph" / "label-catalog.json", self.label_catalog)
        _write_json(
            out / "agents" / "semantic-context.json",
            self.agent_semantic_context,
        )
        artifact_paths = [
            "normalized-contract.json",
            "semantic-model-manifest.json",
            "semantic-crosswalk.json",
            "materialization-plan.json",
            "model-quality-report.json",
            "dependency-graph.json",
            "ontology/model.yaml",
            "ontology/ids.lock.json",
            "graph/semantic-plan.json",
            "graph/label-catalog.json",
            "agents/semantic-context.json",
        ]
        artifacts = [
            {
                "path": relative_path,
                "sha256": _sha256_bytes((out / relative_path).read_bytes()),
            }
            for relative_path in artifact_paths
        ]
        artifact_set_hash = _canonical_hash(artifacts)
        _write_json(
            out / "semantic-manifest.json",
            {
                "schema_version": "1.1",
                "contract_hash": self.contract_hash,
                "semantic_model_manifest_hash": (
                    self.semantic_model_manifest.manifest_hash
                ),
                "semantic_crosswalk_hash": _canonical_hash(
                    self.semantic_crosswalk.model_dump(mode="json")
                ),
                "materialization_plan_hash": _canonical_hash(
                    self.materialization_plan.model_dump(mode="json")
                ),
                "model_quality_report_hash": (
                    self.model_quality_report.report_hash
                ),
                "dependency_graph_hash": self.dependency_graph.graph_hash,
                "artifact_set_hash": artifact_set_hash,
                "artifacts": artifacts,
            },
        )
        return out


_PROPERTY_TYPE_MAP = {
    "string": "string",
    "integer": "int",
    "number": "double",
    "boolean": "boolean",
    "datetime": "timestamp",
    "date": "string",
    "uri": "blob_url",
    "json": "string",
}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _canonical_mappings_payload(bundle: SemanticBundle) -> dict[str, Any]:
    payload = bundle.mappings.model_dump(mode="json")
    payload["entity_types"] = sorted(
        payload.get("entity_types", []),
        key=lambda item: item["semantic_id"],
    )
    payload["relationship_types"] = sorted(
        payload.get("relationship_types", []),
        key=lambda item: item["semantic_id"],
    )
    return payload


def _graph_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_")
    if not label:
        raise SemanticCompileError(f"Cannot derive a Graph label from '{value}'.")
    if label[0].isdigit():
        label = f"GraphItem_{label}"
    if label.casefold() in FABRIC_RESERVED_PHYSICAL_IDENTIFIERS:
        label = f"{label}Entity"
    return label


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", value.casefold()).strip("_")
    return slug or "semantic_item"


def _materialized_table(prefix: str, semantic_id: str) -> str:
    name = f"kg_{prefix}_{_slug(semantic_id.split(':', 1)[-1])}"
    if len(name) <= 120:
        return name
    suffix = hashlib.sha256(semantic_id.encode("utf-8")).hexdigest()[:12]
    return f"{name[:107]}_{suffix}"


def _stable_text_id(prefix: str, semantic_id: str) -> str:
    digest = hashlib.sha256(semantic_id.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _stable_guid(namespace: str, semantic_id: str) -> str:
    root = uuid.UUID("a4f76268-4ca8-5c75-a338-d0ccf3d43a3b")
    return str(uuid.uuid5(root, f"{namespace}:{semantic_id}"))


def _allocate_property_ids(
    property_keys: list[tuple[str, str]],
    used_ids: set[str],
) -> dict[tuple[str, str], str]:
    allocated: dict[tuple[str, str], str] = {}
    for owner_type_id, property_id in sorted(property_keys):
        salt = 0
        while True:
            seed = f"{owner_type_id}/{property_id}/{salt}".encode("utf-8")
            value = 2_000_000_000_000_000_000 + (
                int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
                % 7_000_000_000_000_000_000
            )
            candidate = str(value)
            if candidate not in used_ids:
                used_ids.add(candidate)
                allocated[(owner_type_id, property_id)] = candidate
                break
            salt += 1
    return allocated


def _hierarchy_depth(
    semantic_id: str,
    parent_by_id: Mapping[str, str | None],
) -> int:
    depth = 0
    cursor = parent_by_id.get(semantic_id)
    while cursor is not None:
        depth += 1
        cursor = parent_by_id.get(cursor)
    return depth


def _ontology_property(prop: ManifestPropertyEntry) -> dict[str, Any]:
    return {
        "id": prop.ontology_projection.ontology_property_id,
        "name": prop.name,
        "type": _PROPERTY_TYPE_MAP[prop.value_type],
        "required": prop.required,
        "description": prop.business_description,
    }


def _entity_binding(
    entity: ManifestEntityTypeEntry,
    properties: list[ManifestPropertyEntry],
    table_spec: EntityTableSpec,
) -> dict[str, Any]:
    return {
        "table": entity.physical_source_table,
        "bindingId": entity.ontology_projection.binding_id,
        "entityIdColumn": table_spec.entity_id_column,
        "displayNameColumn": table_spec.display_name_column,
        "additionalColumns": [
            {
                "property": prop.name,
                "column": prop.physical_source_column or prop.name,
            }
            for prop in sorted(properties, key=lambda item: item.property_id)
        ],
    }


def _relationship_binding(
    relationship: ManifestRelationshipEntry,
    table_spec: RelationshipTableSpec,
) -> dict[str, Any]:
    binding: dict[str, Any] = {
        "table": relationship.physical_source_table,
        "contextualizationId": (
            relationship.ontology_projection.contextualization_id
        ),
        "relationshipIdColumn": table_spec.relationship_id_column,
        "sourceEntityIdColumn": relationship.source_endpoint_column,
        "targetEntityIdColumn": relationship.target_endpoint_column,
    }
    if table_spec.evidence_column:
        binding["evidenceIdColumn"] = table_spec.evidence_column
    return binding


def _read_source_counts(
    bundle: SemanticBundle,
    data_dir: Path | None,
) -> dict[str, tuple[int | None, str]]:
    if data_dir is None:
        return {}
    try:
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise SemanticCompileError(
            "Observed semantic availability requires pyarrow."
        ) from exc

    result: dict[str, tuple[int | None, str]] = {}
    cache: dict[tuple[Path, str | None], Any] = {}
    mappings = [
        *bundle.mappings.entity_types,
        *bundle.mappings.relationship_types,
    ]
    for mapping in mappings:
        try:
            path = resolve_semantic_source_parquet(
                data_dir,
                mapping.table,
            )
        except FileNotFoundError:
            result[mapping.semantic_id] = (None, "unavailable")
            continue
        except ValueError as exc:
            raise SemanticCompileError(str(exc)) from exc
        try:
            filter_column = mapping.type_filter_column
            cache_key = (path, filter_column)
            table = cache.get(cache_key)
            if table is None:
                columns = [filter_column] if filter_column else []
                table = pq.read_table(path, columns=columns or None)
                cache[cache_key] = table
            if mapping.type_filter_column:
                if mapping.type_filter_column not in table.column_names:
                    raise SemanticCompileError(
                        f"Observed table '{path}' is missing filter column "
                        f"'{mapping.type_filter_column}'."
                    )
                mask = pc.equal(
                    table[mapping.type_filter_column],
                    mapping.type_filter_value,
                )
                count = int(pc.sum(pc.cast(mask, "int64")).as_py() or 0)
            else:
                count = int(table.num_rows)
        except SemanticCompileError:
            raise
        except Exception as exc:
            raise SemanticCompileError(
                f"Could not inspect observed semantic table '{path}': {exc}"
            ) from exc
        result[mapping.semantic_id] = (count, "sufficient")
    return result


def _quality_metrics(
    quality_report: EnrichmentQualityReport | None,
) -> ModelQualityMetrics:
    if quality_report is None:
        return ModelQualityMetrics(
            canonical_id_crosswalk_coverage=1.0,
            agent_property_projection_coverage=1.0,
            relationship_endpoint_coverage=1.0,
            accepted_property_evidence_coverage=0.0,
            accepted_relationship_evidence_coverage=0.0,
        )
    property_accepted = int(quality_report.property_counts.get("accepted", 0))
    relationship_accepted = int(
        quality_report.relationship_counts.get("accepted", 0)
    )
    missing_evidence = round(
        property_accepted * (1 - quality_report.property_evidence_coverage)
        + relationship_accepted
        * (1 - quality_report.relationship_evidence_coverage)
    )
    return ModelQualityMetrics(
        duplicate_description_count=len(
            quality_report.duplicate_description_findings
        ),
        missing_evidence_count=missing_evidence,
        discovery_type_count=int(
            quality_report.entity_counts.get("discovery", 0)
        ),
        discovery_relationship_count=int(
            quality_report.relationship_counts.get("discovery", 0)
        ),
        canonical_id_crosswalk_coverage=1.0,
        agent_property_projection_coverage=1.0,
        relationship_endpoint_coverage=(
            quality_report.relationship_endpoint_resolution
        ),
        accepted_property_evidence_coverage=(
            quality_report.property_evidence_coverage
        ),
        accepted_relationship_evidence_coverage=(
            quality_report.relationship_evidence_coverage
        ),
    )


def _parse_quality_report(
    value: EnrichmentQualityReport | Mapping[str, Any] | None,
    *,
    contract_hash: str,
) -> EnrichmentQualityReport | None:
    if value is None:
        return None
    try:
        report = (
            value
            if isinstance(value, EnrichmentQualityReport)
            else EnrichmentQualityReport.model_validate(value)
        )
    except ValidationError as exc:
        raise SemanticCompileError(
            f"Invalid semantic quality report: {exc}"
        ) from exc
    if report.status != "passed":
        raise SemanticCompileError(
            "Semantic model compilation requires a passed enrichment quality "
            "report."
        )
    if (
        report.semantic_contract_hash is not None
        and report.semantic_contract_hash != contract_hash
    ):
        raise SemanticCompileError(
            "Semantic quality report contract hash does not match the "
            "approved semantic contract."
        )
    return report


def _build_manifest(
    bundle: SemanticBundle,
    *,
    data_version: str,
    source_counts: Mapping[str, tuple[int | None, str]],
    quality_report: EnrichmentQualityReport | None,
) -> tuple[
    SemanticModelManifest,
    SemanticCrosswalk,
    MaterializationPlan,
    list[ModelQualityFinding],
]:
    entity_mappings = {
        mapping.semantic_id: mapping for mapping in bundle.mappings.entity_types
    }
    relationship_mappings = {
        mapping.semantic_id: mapping
        for mapping in bundle.mappings.relationship_types
    }
    selected_entities = sorted(
        (
            entity
            for entity in bundle.contract.entity_types
            if entity.publication_status != "excluded"
            and entity.id in entity_mappings
        ),
        key=lambda entity: entity.id,
    )
    selected_entity_ids = {entity.id for entity in selected_entities}
    selected_relationships = sorted(
        (
            relationship
            for relationship in bundle.contract.relationship_types
            if relationship.publication_status != "excluded"
            and relationship.id in relationship_mappings
            and relationship.source_type in selected_entity_ids
            and relationship.target_type in selected_entity_ids
        ),
        key=lambda relationship: relationship.id,
    )
    selected_relationship_predicates = {
        relationship.predicate for relationship in selected_relationships
    }
    dangling_materialized_inverses = [
        relationship.id
        for relationship in selected_relationships
        if relationship.inverse is not None
        and relationship.inverse.materialization == "materialized"
        and relationship.inverse.predicate
        not in selected_relationship_predicates
    ]
    if dangling_materialized_inverses:
        raise SemanticCompileError(
            "Materialized inverse relationships must reference another "
            "published relationship predicate: "
            f"{dangling_materialized_inverses}."
        )
    missing_relationship_endpoints = [
        relationship.id
        for relationship in bundle.contract.relationship_types
        if relationship.publication_status == "core"
        and relationship.id in relationship_mappings
        and (
            relationship.source_type not in selected_entity_ids
            or relationship.target_type not in selected_entity_ids
        )
    ]
    if missing_relationship_endpoints:
        raise SemanticCompileError(
            "Core relationships have unpublished or unmapped endpoints: "
            f"{missing_relationship_endpoints}."
        )

    node_labels = {
        entity.id: _graph_label(entity.name) for entity in selected_entities
    }
    edge_labels = {
        relationship.id: _graph_label(relationship.predicate)
        for relationship in selected_relationships
    }
    labels = [*node_labels.values(), *edge_labels.values()]
    if len(labels) != len(set(labels)):
        raise SemanticCompileError(
            "Entity and relationship Graph labels collide after normalization. "
            "Rename the conflicting semantic definitions explicitly."
        )

    lock_entity_by_id = {
        binding.semantic_id: binding
        for binding in bundle.ids.entity_types.values()
    }
    lock_relationship_by_id = {
        binding.semantic_id: binding
        for binding in bundle.ids.relationship_types.values()
    }
    for entity in selected_entities:
        binding = lock_entity_by_id.get(entity.id)
        if binding is None or not binding.fabric_id:
            raise SemanticCompileError(
                f"Entity type '{entity.name}' has no preserved Fabric type ID."
            )
    for relationship in selected_relationships:
        binding = lock_relationship_by_id.get(relationship.id)
        if binding is None or not binding.fabric_id:
            raise SemanticCompileError(
                f"Relationship '{relationship.predicate}' has no preserved "
                "Fabric type ID."
            )

    enrichment_context = build_semantic_enrichment_context(bundle)
    property_definitions = []
    property_by_owner_name: dict[tuple[str, str], ManifestPropertyEntry] = {}
    quality_findings: list[ModelQualityFinding] = []
    if quality_report is None:
        quality_findings.append(ModelQualityFinding(
            code="SOURCE_QUALITY_EVIDENCE_NOT_PROVIDED",
            severity="warning",
            message=(
                "Enrichment quality evidence was not provided; accepted-fact "
                "evidence coverage is reported as 0.0 rather than inferred."
            ),
        ))
    used_ontology_ids = {
        str(binding.fabric_id)
        for binding in [
            *lock_entity_by_id.values(),
            *lock_relationship_by_id.values(),
        ]
        if binding.fabric_id
    }
    property_keys = [
        (
            entity.id,
            enrichment_context.properties_by_owner_alias[
                (entity.id, prop.name.casefold())
            ].property_id,
        )
        for entity in selected_entities
        for prop in entity.properties
    ]
    property_ontology_ids = _allocate_property_ids(
        property_keys,
        used_ontology_ids,
    )
    for entity in selected_entities:
        mapping = entity_mappings[entity.id]
        for prop in sorted(entity.properties, key=lambda item: item.name):
            compiled_prop = enrichment_context.properties_by_owner_alias[
                (entity.id, prop.name.casefold())
            ]
            description = prop.description.strip()
            if not description:
                description = entity.description
                quality_findings.append(
                    ModelQualityFinding(
                        code="PROPERTY_DESCRIPTION_INHERITED",
                        severity="warning",
                        semantic_id=compiled_prop.property_id,
                        message=(
                            "Property has no dedicated business description; "
                            "the approved owner-type description is retained "
                            "until the contract is refined."
                        ),
                    )
                )
            physical_column = mapping.property_columns.get(
                prop.name,
                prop.name,
            )
            entry = ManifestPropertyEntry(
                property_id=compiled_prop.property_id,
                owner_type_id=entity.id,
                name=prop.name,
                business_description=description,
                value_type=prop.type,
                unit_policy=compiled_prop.unit_policy,
                required=prop.required,
                agent_visible=prop.name not in entity.lineage_properties,
                evidence_policy=compiled_prop.evidence_policy,
                physical_source_column=physical_column,
                ontology_projection=OntologyPropertyProjection(
                    ontology_property_id=property_ontology_ids[
                        (entity.id, compiled_prop.property_id)
                    ]
                ),
                graph_projection=GraphPropertyProjection(
                    property_key=physical_column
                ),
                agent_projection=AgentPropertyChildProjection(
                    child_id=_stable_text_id(
                        "agent-property",
                        f"{entity.id}/{compiled_prop.property_id}",
                    ),
                    child_name=prop.name,
                ),
            )
            property_definitions.append(entry)
            property_by_owner_name[(entity.id, prop.name)] = entry

    vocabulary_aliases: dict[str, list[str]] = {}
    for term in bundle.vocabulary.terms:
        vocabulary_aliases.setdefault(
            term.preferred_label.casefold(),
            [],
        ).extend(term.aliases)
    parent_by_id = {
        entity.id: entity.parent for entity in selected_entities
    }
    manifest_entities: list[ManifestEntityTypeEntry] = []
    entity_tables: list[EntityTableSpec] = []
    availability: list[DataAvailability] = []
    for entity in selected_entities:
        mapping = entity_mappings[entity.id]
        owner_properties = sorted(
            (
                prop
                for prop in property_definitions
                if prop.owner_type_id == entity.id
            ),
            key=lambda item: item.property_id,
        )
        table_name = _materialized_table("entity", entity.id)
        identifier_ids = [
            property_by_owner_name[(entity.id, name)].property_id
            for name in entity.identifiers
        ]
        aliases = sorted(
            set(entity.aliases)
            | {entity.name}
            | set(vocabulary_aliases.get(entity.name.casefold(), []))
            | set(vocabulary_aliases.get(entity.business_name.casefold(), []))
        )
        ontology_type_id = str(lock_entity_by_id[entity.id].fabric_id)
        column_by_name = {
            prop.physical_source_column or prop.name: ColumnSpec(
                column_name=prop.physical_source_column or prop.name,
                semantic_property_id=prop.property_id,
                data_type=prop.value_type,
                nullable=not prop.required,
            )
            for prop in owner_properties
        }
        column_by_name.setdefault(
            mapping.entity_id_column,
            ColumnSpec(
                column_name=mapping.entity_id_column,
                data_type="string",
                nullable=False,
            ),
        )
        column_by_name.setdefault(
            mapping.display_name_column,
            ColumnSpec(
                column_name=mapping.display_name_column,
                data_type="string",
                nullable=False,
            ),
        )
        columns = [
            column_by_name[column_name]
            for column_name in sorted(column_by_name)
        ]
        semantic_layer = entity.semantic_layer or (
            "common" if entity.abstract or entity.parent is None else "domain"
        )
        manifest_entities.append(
            ManifestEntityTypeEntry(
                semantic_id=entity.id,
                canonical_name=node_labels[entity.id],
                business_name=entity.business_name,
                aliases=aliases,
                description=entity.description,
                identifier_properties=identifier_ids,
                published_properties=[
                    prop.property_id for prop in owner_properties
                ],
                hierarchy=HierarchyMetadata(
                    parent_type_id=entity.parent,
                    depth=_hierarchy_depth(entity.id, parent_by_id),
                    is_abstract=entity.abstract,
                ),
                evidence_policy="optional",
                publication_status=entity.publication_status,
                semantic_layer=semantic_layer,
                physical_source_table=table_name,
                ontology_projection=OntologyEntityProjection(
                    ontology_type_id=ontology_type_id,
                    property_ids={
                        prop.name: (
                            prop.ontology_projection.ontology_property_id
                        )
                        for prop in owner_properties
                    },
                    binding_id=_stable_guid("ontology-binding", entity.id),
                ),
                graph_projection=GraphNodeProjection(
                    label=node_labels[entity.id],
                    alias=f"{node_labels[entity.id]}_nodeType",
                    property_keys=[
                        column.column_name for column in columns
                    ],
                ),
                search_linkage=SearchLinkageSpec(
                    index_name="kg-chunks",
                    entity_id_field="entity_ids",
                    type_filter_field="semantic_type_ids",
                    type_filter_value=entity.id,
                ),
                agent_projection=AgentElementProjection(
                    element_id=_stable_text_id("agent-entity", entity.id),
                    element_name=entity.business_name,
                    element_category="entity",
                ),
            )
        )
        entity_tables.append(
            EntityTableSpec(
                semantic_id=entity.id,
                table_name=table_name,
                source_table_name=mapping.table,
                source_filter_column=mapping.type_filter_column,
                source_filter_value=mapping.type_filter_value,
                required=True,
                approval_state=(
                    "approved"
                    if entity.publication_status in {"core", "optional"}
                    else "discovery"
                ),
                entity_id_column=mapping.entity_id_column,
                display_name_column=mapping.display_name_column,
                columns=columns,
            )
        )
        observed_rows, status = source_counts.get(
            entity.id,
            (None, "not_observed"),
        )
        availability.append(
            DataAvailability(
                semantic_id=entity.id,
                observed_rows=observed_rows,
                required_rows=0,
                status=status,
            )
        )

    manifest_relationships: list[ManifestRelationshipEntry] = []
    relationship_tables: list[RelationshipTableSpec] = []
    relationship_semantics = bundle.contract.metadata.get(
        "relationship_semantics",
        {},
    )
    entity_layer_by_id = {
        entity.semantic_id: entity.semantic_layer
        for entity in manifest_entities
    }
    for relationship in selected_relationships:
        mapping = relationship_mappings[relationship.id]
        source_ontology_id = str(
            lock_entity_by_id[relationship.source_type].fabric_id
        )
        target_ontology_id = str(
            lock_entity_by_id[relationship.target_type].fabric_id
        )
        ontology_rel_id = str(
            lock_relationship_by_id[relationship.id].fabric_id
        )
        table_name = _materialized_table(
            "relationship",
            relationship.id,
        )
        metadata = relationship_semantics.get(
            relationship.id,
            relationship_semantics.get(relationship.predicate, {}),
        )
        if not isinstance(metadata, dict):
            raise SemanticCompileError(
                f"relationship_semantics entry for '{relationship.id}' "
                "must be an object."
            )
        semantic_layer = relationship.semantic_layer or (
            "common"
            if entity_layer_by_id.get(relationship.source_type) == "common"
            and entity_layer_by_id.get(relationship.target_type) == "common"
            else "domain"
        )
        manifest_relationships.append(
            ManifestRelationshipEntry(
                semantic_id=relationship.id,
                predicate=relationship.predicate,
                business_name=relationship.business_name,
                description=relationship.description,
                source_type_id=relationship.source_type,
                target_type_id=relationship.target_type,
                direction=relationship.direction,
                cardinality=relationship.cardinality,
                optional=relationship.publication_status != "core",
                inverse_policy=InversePolicy(
                    predicate=(
                        relationship.inverse.predicate
                        if relationship.inverse
                        else None
                    ),
                    materialization=(
                        relationship.inverse.materialization
                        if relationship.inverse
                        else "none"
                    ),
                ),
                transitivity_policy=TransitivityPolicy(
                    transitive=bool(metadata.get("transitive", False)),
                    closure_via=str(metadata.get("closure_via", "none")),
                ),
                assertion_policy=relationship.assertion_policy,
                evidence_policy=relationship.evidence_policy,
                publication_status=relationship.publication_status,
                semantic_layer=semantic_layer,
                physical_source_table=table_name,
                source_endpoint_column=mapping.source_entity_id_column,
                target_endpoint_column=mapping.target_entity_id_column,
                ontology_projection=OntologyRelationshipProjection(
                    ontology_rel_type_id=ontology_rel_id,
                    source_ontology_type_id=source_ontology_id,
                    target_ontology_type_id=target_ontology_id,
                    contextualization_id=_stable_guid(
                        "ontology-context",
                        relationship.id,
                    ),
                ),
                graph_projection=GraphEdgeProjection(
                    label=edge_labels[relationship.id],
                    alias=f"{edge_labels[relationship.id]}_edgeType",
                    source_label=node_labels[relationship.source_type],
                    target_label=node_labels[relationship.target_type],
                ),
                agent_projection=AgentElementProjection(
                    element_id=_stable_text_id(
                        "agent-relationship",
                        relationship.id,
                    ),
                    element_name=relationship.business_name,
                    element_category="relationship",
                ),
            )
        )
        relationship_tables.append(
            RelationshipTableSpec(
                semantic_id=relationship.id,
                table_name=table_name,
                source_table_name=mapping.table,
                source_filter_column=mapping.type_filter_column,
                source_filter_value=mapping.type_filter_value,
                required=True,
                approval_state=(
                    "approved"
                    if relationship.publication_status in {"core", "optional"}
                    else "discovery"
                ),
                relationship_id_column=mapping.relationship_id_column,
                source_column=mapping.source_entity_id_column,
                target_column=mapping.target_entity_id_column,
                evidence_column=mapping.evidence_id_column,
                columns=[
                    ColumnSpec(
                        column_name=mapping.relationship_id_column,
                        data_type="string",
                        nullable=False,
                    ),
                    ColumnSpec(
                        column_name=mapping.source_entity_id_column,
                        data_type="string",
                        nullable=False,
                    ),
                    ColumnSpec(
                        column_name=mapping.target_entity_id_column,
                        data_type="string",
                        nullable=False,
                    ),
                    *(
                        [
                            ColumnSpec(
                                column_name=mapping.evidence_id_column,
                                data_type="string",
                                nullable=(
                                    relationship.evidence_policy
                                    != "required_for_asserted"
                                ),
                            )
                        ]
                        if mapping.evidence_id_column
                        else []
                    ),
                ],
            )
        )
        observed_rows, status = source_counts.get(
            relationship.id,
            (None, "not_observed"),
        )
        availability.append(
            DataAvailability(
                semantic_id=relationship.id,
                observed_rows=observed_rows,
                required_rows=0,
                status=status,
            )
        )

    stable_id_lock_hash = _canonical_hash(
        bundle.ids.model_dump(mode="json")
    )
    metrics = _quality_metrics(quality_report)
    manifest = SemanticModelManifest(
        semantic_contract_hash=bundle.contract_hash,
        stable_id_lock_hash=stable_id_lock_hash,
        data_version=data_version,
        entity_types=manifest_entities,
        property_definitions=property_definitions,
        relationship_types=manifest_relationships,
        publication_profile=PublicationProfile(
            ontology_enabled=True,
            graph_enabled=True,
            search_enabled=True,
            agent_enabled=True,
            published_entity_type_count=len(manifest_entities),
            published_relationship_count=len(manifest_relationships),
            published_property_count=len(property_definitions),
        ),
        competency_coverage=[],
        model_quality=metrics,
    )
    manifest = manifest.model_copy(
        update={"manifest_hash": compute_manifest_hash(manifest)}
    )

    entity_by_id = {
        entity.semantic_id: entity for entity in manifest.entity_types
    }
    relationship_by_id = {
        relationship.semantic_id: relationship
        for relationship in manifest.relationship_types
    }
    crosswalk = SemanticCrosswalk(
        manifest_hash=manifest.manifest_hash,
        entity_type_entries=[
            CrosswalkEntry(
                semantic_id=entity.semantic_id,
                element_kind="entity_type",
                ontology_type_id=(
                    entity.ontology_projection.ontology_type_id
                ),
                graph_label=entity.graph_projection.label,
                graph_alias=entity.graph_projection.alias,
                data_agent_element_id=(
                    entity.agent_projection.element_id
                ),
                search_field_or_filter=(
                    entity.search_linkage.type_filter_field
                ),
                physical_table=entity.physical_source_table,
            )
            for entity in manifest.entity_types
        ],
        relationship_type_entries=[
            CrosswalkEntry(
                semantic_id=relationship.semantic_id,
                element_kind="relationship_type",
                source_type_id=relationship.source_type_id,
                target_type_id=relationship.target_type_id,
                ontology_type_id=(
                    relationship.ontology_projection.ontology_rel_type_id
                ),
                graph_label=relationship.graph_projection.label,
                graph_alias=relationship.graph_projection.alias,
                data_agent_element_id=(
                    relationship.agent_projection.element_id
                ),
                search_field_or_filter=(
                    f"relationship:{relationship.semantic_id}"
                ),
                physical_table=relationship.physical_source_table,
                direction=relationship.direction,
            )
            for relationship in manifest.relationship_types
        ],
        property_entries=[
            CrosswalkEntry(
                semantic_id=prop.property_id,
                element_kind="property",
                owner_type_id=prop.owner_type_id,
                ontology_type_id=(
                    prop.ontology_projection.ontology_property_id
                ),
                graph_label=prop.graph_projection.property_key,
                graph_alias=None,
                data_agent_element_id=(
                    prop.agent_projection.child_id
                    if prop.agent_visible
                    else None
                ),
                search_field_or_filter=(
                    prop.physical_source_column or prop.property_id
                ),
                physical_table=(
                    entity_by_id[prop.owner_type_id].physical_source_table
                ),
            )
            for prop in manifest.property_definitions
        ],
    )
    plan = MaterializationPlan(
        manifest_hash=manifest.manifest_hash,
        entity_tables=entity_tables,
        relationship_tables=relationship_tables,
        data_availability=availability,
        blocked_competencies=[],
    )
    findings = [
        *validate_manifest_model_completeness(manifest, crosswalk),
        *validate_crosswalk_against_manifest(crosswalk, manifest),
        *validate_materialization_availability(plan, manifest),
    ]
    if findings:
        raise SemanticCompileError(
            "Compiled semantic authority failed validation: "
            + "; ".join(
                f"{finding.code}: {finding.message}"
                for finding in findings
            )
        )
    return manifest, crosswalk, plan, quality_findings


def build_ontology_projection(
    manifest: SemanticModelManifest,
    plan: MaterializationPlan,
    *,
    ontology_name: str | None,
    contract_name: str,
    contract_description: str,
    contract_version: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    properties_by_owner: dict[str, list[ManifestPropertyEntry]] = {}
    for prop in manifest.property_definitions:
        properties_by_owner.setdefault(prop.owner_type_id, []).append(prop)
    relationship_table_by_id = {
        table.semantic_id: table for table in plan.relationship_tables
    }
    entity_table_by_id = {
        table.semantic_id: table for table in plan.entity_tables
    }
    entity_name_by_id = {
        entity.semantic_id: entity.canonical_name
        for entity in manifest.entity_types
    }
    common_entities = [
        entity.canonical_name
        for entity in manifest.entity_types
        if entity.semantic_layer == "common"
    ]
    domain_entities = [
        entity.canonical_name
        for entity in manifest.entity_types
        if entity.semantic_layer == "domain"
    ]
    common_relationships = [
        relationship.predicate
        for relationship in manifest.relationship_types
        if relationship.semantic_layer == "common"
    ]
    domain_relationships = [
        relationship.predicate
        for relationship in manifest.relationship_types
        if relationship.semantic_layer == "domain"
    ]

    def physical_entity_id_property(entity_id: str) -> str:
        table = entity_table_by_id[entity_id]
        for prop in properties_by_owner.get(entity_id, []):
            if (
                prop.physical_source_column or prop.name
            ) == table.entity_id_column:
                return prop.name
        raise SemanticCompileError(
            f"Entity '{entity_id}' has no property mapped to physical entity "
            f"ID column '{table.entity_id_column}'."
        )

    ontology_model = {
        "name": ontology_name if ontology_name is not None else contract_name,
        "description": contract_description,
        "version": contract_version,
        "modules": [
            {
                "name": "common-entities",
                "description": (
                    "Reusable thing and concept types shared across domains."
                ),
                "entityTypeNames": common_entities,
                "relationshipTypeNames": [],
            },
            {
                "name": "common-relationships",
                "description": (
                    "Reusable directed verbs connecting common semantic types."
                ),
                "entityTypeNames": common_entities,
                "relationshipTypeNames": common_relationships,
            },
            {
                "name": "domain",
                "description": (
                    "Domain-specific nouns and directed verbs projected from "
                    "the sealed semantic model manifest."
                ),
                "entityTypeNames": domain_entities,
                "relationshipTypeNames": domain_relationships,
            },
        ],
        "entityTypes": [
            {
                "name": entity.canonical_name,
                "description": entity.description,
                "module": (
                    "common-entities"
                    if entity.semantic_layer == "common"
                    else "domain"
                ),
                "properties": [
                    _ontology_property(prop)
                    for prop in properties_by_owner.get(
                        entity.semantic_id,
                        [],
                    )
                ],
                "entityIdProperties": [
                    physical_entity_id_property(entity.semantic_id)
                ],
                "displayNameProperty": next(
                    prop.name
                    for prop in properties_by_owner.get(
                        entity.semantic_id,
                        [],
                    )
                    if (
                        prop.physical_source_column or prop.name
                    )
                    == entity_table_by_id[
                        entity.semantic_id
                    ].display_name_column
                ),
                "dataBinding": _entity_binding(
                    entity,
                    properties_by_owner.get(entity.semantic_id, []),
                    entity_table_by_id[entity.semantic_id],
                ),
            }
            for entity in manifest.entity_types
        ],
        "relationshipTypes": [
            {
                "name": relationship.predicate,
                "description": relationship.description,
                "module": (
                    "common-relationships"
                    if relationship.semantic_layer == "common"
                    else "domain"
                ),
                "sourceType": entity_name_by_id[
                    relationship.source_type_id
                ],
                "targetType": entity_name_by_id[
                    relationship.target_type_id
                ],
                "inversePolicy": (
                    "materialize"
                    if relationship.inverse_policy.materialization
                    == "materialized"
                    else "none"
                ),
                "inverseName": relationship.inverse_policy.predicate,
                "dataBinding": _relationship_binding(
                    relationship,
                    relationship_table_by_id[relationship.semantic_id],
                ),
            }
            for relationship in manifest.relationship_types
        ],
    }
    ontology_ids_lock = {
        "_comment": (
            "Compiled from the canonical semantic stable ID lock and sealed "
            "semantic model manifest. IDs must not be regenerated."
        ),
        "contractHash": manifest.semantic_contract_hash,
        "semanticModelManifestHash": manifest.manifest_hash,
        "entityTypes": {
            entity.canonical_name: (
                entity.ontology_projection.ontology_type_id
            )
            for entity in manifest.entity_types
        },
        "relationshipTypes": {
            relationship.predicate: (
                relationship.ontology_projection.ontology_rel_type_id
            )
            for relationship in manifest.relationship_types
        },
        "properties": {
            f"{prop.owner_type_id}/{prop.property_id}": (
                prop.ontology_projection.ontology_property_id
            )
            for prop in manifest.property_definitions
        },
    }
    return ontology_model, ontology_ids_lock


def build_graph_projection(
    manifest: SemanticModelManifest,
    materialization_plan: MaterializationPlan,
) -> tuple[
    tuple[str, ...],
    dict[str, str],
    tuple[dict[str, Any], ...],
    dict[str, Any],
]:
    entity_name_by_id = {
        entity.semantic_id: entity.canonical_name
        for entity in manifest.entity_types
    }
    graph_node_labels = {
        entity.canonical_name: str(entity.graph_projection.label)
        for entity in manifest.entity_types
    }
    relationship_table_by_id = {
        table.semantic_id: table
        for table in materialization_plan.relationship_tables
    }
    graph_relationships = tuple(
        {
            "semantic_id": relationship.semantic_id,
            "name": relationship.predicate,
            "graph_label": relationship.graph_projection.label,
            "graph_alias": relationship.graph_projection.alias,
            "source_type": entity_name_by_id[
                relationship.source_type_id
            ],
            "target_type": entity_name_by_id[
                relationship.target_type_id
            ],
            "table": relationship.physical_source_table,
            "type_filter_column": None,
            "type_filter_value": None,
            "source_entity_id_column": (
                relationship.source_endpoint_column
            ),
            "target_entity_id_column": (
                relationship.target_endpoint_column
            ),
            "evidence_id_column": relationship_table_by_id[
                relationship.semantic_id
            ].evidence_column,
            "property_columns": [
                column.column_name
                for column in relationship_table_by_id[
                    relationship.semantic_id
                ].columns
            ],
        }
        for relationship in manifest.relationship_types
    )
    label_catalog = {
        "schema_version": "1.1",
        "contract_hash": manifest.semantic_contract_hash,
        "semantic_model_manifest_hash": manifest.manifest_hash,
        "nodes": [
            {
                "semantic_id": entity.semantic_id,
                "entity_name": entity.canonical_name,
                "business_name": entity.business_name,
                "graph_label": entity.graph_projection.label,
                "graph_alias": entity.graph_projection.alias,
                "publication_status": entity.publication_status,
                "table": entity.physical_source_table,
                "properties": entity.graph_projection.property_keys,
            }
            for entity in manifest.entity_types
        ],
        "edges": [
            {
                "semantic_id": relationship.semantic_id,
                "predicate": relationship.predicate,
                "business_name": relationship.business_name,
                "graph_label": relationship.graph_projection.label,
                "graph_alias": relationship.graph_projection.alias,
                "source_semantic_id": relationship.source_type_id,
                "target_semantic_id": relationship.target_type_id,
                "source_graph_label": (
                    relationship.graph_projection.source_label
                ),
                "target_graph_label": (
                    relationship.graph_projection.target_label
                ),
                "direction": relationship.direction,
                "evidence_policy": relationship.evidence_policy,
                "publication_status": relationship.publication_status,
                "table": relationship.physical_source_table,
            }
            for relationship in manifest.relationship_types
        ],
    }
    return (
        tuple(entity_name_by_id.values()),
        graph_node_labels,
        graph_relationships,
        label_catalog,
    )


def build_agent_semantic_context(
    manifest: SemanticModelManifest,
    crosswalk: SemanticCrosswalk,
    *,
    contract_name: str,
    contract_description: str,
) -> dict[str, Any]:
    """Build the agent-visible schema only from the sealed manifest/crosswalk."""
    entity_crosswalk = {
        entry.semantic_id: entry
        for entry in crosswalk.entity_type_entries
    }
    relationship_crosswalk = {
        entry.semantic_id: entry
        for entry in crosswalk.relationship_type_entries
    }
    properties_by_owner: dict[str, list[ManifestPropertyEntry]] = {}
    for prop in manifest.property_definitions:
        properties_by_owner.setdefault(prop.owner_type_id, []).append(prop)
    return {
        "schema_version": "1.1",
        "contract_hash": manifest.semantic_contract_hash,
        "semantic_model_manifest_hash": manifest.manifest_hash,
        "semantic_crosswalk_hash": _canonical_hash(
            crosswalk.model_dump(mode="json")
        ),
        "contract_name": contract_name,
        "contract_description": contract_description,
        "entity_types": [
            {
                "semantic_id": entity.semantic_id,
                "business_name": entity.business_name,
                "aliases": entity.aliases,
                "description": entity.description,
                "graph_label": entity_crosswalk[
                    entity.semantic_id
                ].graph_label,
                "lakehouse_table": entity.physical_source_table,
                "data_agent_element_id": entity_crosswalk[
                    entity.semantic_id
                ].data_agent_element_id,
                "property_ids": entity.published_properties,
                "properties": [
                    {
                        "semantic_id": prop.property_id,
                        "name": prop.name,
                        "business_description": prop.business_description,
                        "value_type": prop.value_type,
                        "required": prop.required,
                        "evidence_policy": prop.evidence_policy,
                        "graph_property": (
                            prop.graph_projection.property_key
                        ),
                        "data_agent_element_id": (
                            prop.agent_projection.child_id
                        ),
                        "readiness_state": "not_observed",
                    }
                    for prop in sorted(
                        properties_by_owner.get(entity.semantic_id, []),
                        key=lambda item: item.property_id,
                    )
                    if prop.agent_visible
                ],
                "readiness_state": "not_observed",
            }
            for entity in manifest.entity_types
        ],
        "property_definitions": [
            {
                "semantic_id": prop.property_id,
                "owner_type_id": prop.owner_type_id,
                "name": prop.name,
                "business_description": prop.business_description,
                "value_type": prop.value_type,
                "required": prop.required,
                "agent_visible": prop.agent_visible,
                "evidence_policy": prop.evidence_policy,
                "graph_property": prop.graph_projection.property_key,
                "data_agent_element_id": prop.agent_projection.child_id,
                "readiness_state": "not_observed",
            }
            for prop in manifest.property_definitions
        ],
        "relationship_types": [
            {
                "semantic_id": relationship.semantic_id,
                "business_name": relationship.business_name,
                "description": relationship.description,
                "graph_label": relationship_crosswalk[
                    relationship.semantic_id
                ].graph_label,
                "lakehouse_table": relationship.physical_source_table,
                "data_agent_element_id": relationship_crosswalk[
                    relationship.semantic_id
                ].data_agent_element_id,
                "source_graph_label": (
                    relationship.graph_projection.source_label
                ),
                "target_graph_label": (
                    relationship.graph_projection.target_label
                ),
                "source_type_id": relationship.source_type_id,
                "target_type_id": relationship.target_type_id,
                "direction": relationship.direction,
                "cardinality": relationship.cardinality.model_dump(
                    mode="json"
                ),
                "evidence_policy": relationship.evidence_policy,
                "publication_status": relationship.publication_status,
                "optional": relationship.optional,
                "readiness_state": "not_observed",
            }
            for relationship in manifest.relationship_types
        ],
    }


def _build_quality_report(
    manifest: SemanticModelManifest,
    quality_report: EnrichmentQualityReport | None,
    findings: list[ModelQualityFinding],
) -> SemanticModelQualityReport:
    source_hash = (
        _canonical_hash(quality_report.model_dump(mode="json"))
        if quality_report is not None
        else None
    )
    report = SemanticModelQualityReport(
        semantic_contract_hash=manifest.semantic_contract_hash,
        manifest_hash=manifest.manifest_hash,
        status=(
            "failed"
            if any(finding.severity == "error" for finding in findings)
            else "passed"
        ),
        metrics=manifest.model_quality,
        findings=findings,
        source_quality_report_hash=source_hash,
    )
    return report.model_copy(
        update={"report_hash": compute_model_quality_report_hash(report)}
    )


def _build_dependency_graph(
    bundle: SemanticBundle,
    manifest: SemanticModelManifest,
    crosswalk: SemanticCrosswalk,
    plan: MaterializationPlan,
    quality_report: SemanticModelQualityReport,
    ontology_model: Mapping[str, Any],
    label_catalog: Mapping[str, Any],
    agent_context: Mapping[str, Any],
) -> SemanticDependencyGraph:
    hashes = {
        "semantic-contract": bundle.contract_hash,
        "stable-id-lock": manifest.stable_id_lock_hash,
        "physical-mappings": _canonical_hash(
            _canonical_mappings_payload(bundle)
        ),
        "controlled-vocabulary": _canonical_hash(
            bundle.vocabulary.model_dump(mode="json")
        ),
        "canonical-data": _canonical_hash(manifest.data_version),
        "semantic-model-manifest": manifest.manifest_hash,
        "semantic-crosswalk": _canonical_hash(
            crosswalk.model_dump(mode="json")
        ),
        "materialization-plan": _canonical_hash(
            plan.model_dump(mode="json")
        ),
        "model-quality-report": quality_report.report_hash,
        "ontology-projection": _canonical_hash(ontology_model),
        "graph-projection": _canonical_hash(label_catalog),
        "search-projection": _canonical_hash(
            {
                "manifest_hash": manifest.manifest_hash,
                "crosswalk_hash": _canonical_hash(
                    crosswalk.model_dump(mode="json")
                ),
                "linkages": [
                    entity.search_linkage.model_dump(mode="json")
                    for entity in manifest.entity_types
                ],
            }
        ),
        "agent-schema": _canonical_hash(agent_context),
    }
    downstream = {
        "semantic-contract": [
            "semantic-model-manifest",
            "semantic-crosswalk",
            "materialization-plan",
            "model-quality-report",
            "ontology-projection",
            "graph-projection",
            "search-projection",
            "agent-schema",
        ],
        "stable-id-lock": [
            "semantic-model-manifest",
            "semantic-crosswalk",
            "ontology-projection",
            "graph-projection",
            "agent-schema",
        ],
        "physical-mappings": [
            "semantic-model-manifest",
            "semantic-crosswalk",
            "materialization-plan",
            "ontology-projection",
            "graph-projection",
            "search-projection",
            "agent-schema",
        ],
        "controlled-vocabulary": [
            "semantic-model-manifest",
            "semantic-crosswalk",
            "ontology-projection",
            "graph-projection",
            "agent-schema",
        ],
        "canonical-data": [
            "semantic-model-manifest",
            "materialization-plan",
            "model-quality-report",
            "ontology-projection",
            "graph-projection",
            "search-projection",
            "agent-schema",
        ],
        "semantic-model-manifest": [
            "semantic-crosswalk",
            "materialization-plan",
            "model-quality-report",
            "ontology-projection",
            "graph-projection",
            "search-projection",
            "agent-schema",
        ],
        "semantic-crosswalk": [
            "ontology-projection",
            "graph-projection",
            "search-projection",
            "agent-schema",
        ],
        "materialization-plan": [
            "ontology-projection",
            "graph-projection",
        ],
        "model-quality-report": [],
        "ontology-projection": ["agent-schema"],
        "graph-projection": ["agent-schema"],
        "search-projection": ["agent-schema"],
        "agent-schema": [],
    }
    dependencies = {
        "semantic-contract": [],
        "stable-id-lock": [],
        "physical-mappings": [],
        "controlled-vocabulary": [],
        "canonical-data": [],
        "semantic-model-manifest": [
            "semantic-contract",
            "stable-id-lock",
            "physical-mappings",
            "controlled-vocabulary",
            "canonical-data",
        ],
        "semantic-crosswalk": ["semantic-model-manifest"],
        "materialization-plan": [
            "semantic-model-manifest",
            "physical-mappings",
            "canonical-data",
        ],
        "model-quality-report": [
            "semantic-model-manifest",
            "canonical-data",
        ],
        "ontology-projection": [
            "semantic-model-manifest",
            "semantic-crosswalk",
            "materialization-plan",
        ],
        "graph-projection": [
            "semantic-model-manifest",
            "semantic-crosswalk",
            "materialization-plan",
        ],
        "search-projection": [
            "semantic-model-manifest",
            "semantic-crosswalk",
        ],
        "agent-schema": [
            "semantic-model-manifest",
            "semantic-crosswalk",
            "ontology-projection",
            "graph-projection",
            "search-projection",
        ],
    }
    graph = SemanticDependencyGraph(
        semantic_contract_hash=bundle.contract_hash,
        manifest_hash=manifest.manifest_hash,
        nodes=[
            DependencyNode(
                artifact_id=artifact_id,
                artifact_hash=hashes[artifact_id],
                depends_on=dependencies[artifact_id],
                invalidates=downstream[artifact_id],
            )
            for artifact_id in sorted(hashes)
        ],
    )
    return graph.model_copy(
        update={"graph_hash": compute_dependency_graph_hash(graph)}
    )


def compile_semantic_bundle(
    bundle: SemanticBundle,
    *,
    ontology_name: str | None = None,
    data_version: str = "not-observed",
    data_dir: Path | str | None = None,
    quality_report: EnrichmentQualityReport | Mapping[str, Any] | None = None,
) -> CompiledSemanticArtifacts:
    """Compile one approved bundle into a single sealed semantic authority."""
    verified_hash = validate_semantic_bundle(
        bundle.contract,
        bundle.mappings,
        bundle.vocabulary,
        bundle.ids,
        require_approval=True,
    )
    if verified_hash != bundle.contract_hash:
        raise SemanticCompileError(
            "Semantic bundle contract hash does not match its validated content."
        )
    parsed_quality = _parse_quality_report(
        quality_report,
        contract_hash=verified_hash,
    )
    source_counts = _read_source_counts(
        bundle,
        Path(data_dir) if data_dir is not None else None,
    )
    manifest, crosswalk, plan, quality_findings = _build_manifest(
        bundle,
        data_version=data_version,
        source_counts=source_counts,
        quality_report=parsed_quality,
    )
    ontology_model, ontology_ids_lock = build_ontology_projection(
        manifest,
        plan,
        ontology_name=ontology_name,
        contract_name=bundle.contract.name,
        contract_description=bundle.contract.description,
        contract_version=bundle.contract.contract_version,
    )
    (
        graph_entity_types,
        graph_node_labels,
        graph_relationships,
        label_catalog,
    ) = build_graph_projection(manifest, plan)
    agent_context = build_agent_semantic_context(
        manifest,
        crosswalk,
        contract_name=bundle.contract.name,
        contract_description=bundle.contract.description,
    )
    model_quality_report = _build_quality_report(
        manifest,
        parsed_quality,
        quality_findings,
    )
    dependency_graph = _build_dependency_graph(
        bundle,
        manifest,
        crosswalk,
        plan,
        model_quality_report,
        ontology_model,
        label_catalog,
        agent_context,
    )
    return CompiledSemanticArtifacts(
        contract_hash=verified_hash,
        normalized_contract=normalize_semantic_contract(bundle.contract),
        semantic_model_manifest=manifest,
        semantic_crosswalk=crosswalk,
        materialization_plan=plan,
        model_quality_report=model_quality_report,
        dependency_graph=dependency_graph,
        ontology_model=ontology_model,
        ontology_ids_lock=ontology_ids_lock,
        graph_entity_types=graph_entity_types,
        graph_node_labels=graph_node_labels,
        graph_relationships=graph_relationships,
        label_catalog=label_catalog,
        agent_semantic_context=agent_context,
    )


def load_semantic_model_artifacts(
    input_dir: Path | str,
) -> LoadedSemanticModelArtifacts:
    """Load and validate the persisted H2 semantic authority."""
    root = Path(input_dir)
    try:
        integration_manifest = json.loads(
            (root / "semantic-manifest.json").read_text(encoding="utf-8")
        )
        manifest = SemanticModelManifest.model_validate_json(
            (root / "semantic-model-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        crosswalk = SemanticCrosswalk.model_validate_json(
            (root / "semantic-crosswalk.json").read_text(encoding="utf-8")
        )
        plan = MaterializationPlan.model_validate_json(
            (root / "materialization-plan.json").read_text(encoding="utf-8")
        )
        quality_report = SemanticModelQualityReport.model_validate_json(
            (root / "model-quality-report.json").read_text(encoding="utf-8")
        )
        dependency_graph = SemanticDependencyGraph.model_validate_json(
            (root / "dependency-graph.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise SemanticCompileError(
            f"Could not load sealed semantic artifacts from '{root}': {exc}"
        ) from exc
    if not isinstance(integration_manifest, dict):
        raise SemanticCompileError(
            "semantic-manifest.json must contain a JSON object."
        )
    artifact_payload = integration_manifest.get("artifacts")
    if isinstance(artifact_payload, list):
        artifact_hashes = {
            str(item.get("path")): str(item.get("sha256"))
            for item in artifact_payload
            if isinstance(item, dict)
            and item.get("path")
            and item.get("sha256")
        }
    elif isinstance(artifact_payload, dict):
        artifact_hashes = {
            str(relative_path): str(digest)
            for relative_path, digest in artifact_payload.items()
        }
    else:
        raise SemanticCompileError(
            "semantic-manifest.json must enumerate sealed artifacts."
        )
    required_paths = {
        "normalized-contract.json",
        "semantic-model-manifest.json",
        "semantic-crosswalk.json",
        "materialization-plan.json",
        "model-quality-report.json",
        "dependency-graph.json",
    }
    missing_paths = sorted(required_paths - set(artifact_hashes))
    if missing_paths:
        raise SemanticCompileError(
            "semantic-manifest.json omits required sealed artifacts: "
            f"{missing_paths}."
        )
    root_resolved = root.resolve()
    for relative_path, expected_hash in artifact_hashes.items():
        candidate_relative = Path(relative_path)
        if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
            raise SemanticCompileError(
                "semantic-manifest.json contains an unsafe artifact path: "
                f"{relative_path!r}."
            )
        candidate = (root / candidate_relative).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise SemanticCompileError(
                "semantic-manifest.json artifact path escapes the semantic "
                f"directory: {relative_path!r}."
            ) from exc
        if not candidate.is_file():
            raise SemanticCompileError(
                f"Sealed semantic artifact is missing: {relative_path}."
            )
        actual_hash = _sha256_bytes(candidate.read_bytes())
        if actual_hash != expected_hash:
            raise SemanticCompileError(
                f"Sealed semantic artifact hash mismatch for "
                f"'{relative_path}': expected {expected_hash}, "
                f"found {actual_hash}."
            )
    findings = [
        *validate_manifest_model_completeness(manifest, crosswalk),
        *validate_crosswalk_against_manifest(crosswalk, manifest),
        *validate_materialization_availability(plan, manifest),
    ]
    if manifest.manifest_hash != compute_manifest_hash(manifest):
        findings.append(
            ArtifactFinding(
                "MANIFEST_HASH_INVALID",
                "Persisted manifest hash does not match content.",
            )
        )
    expected_crosswalk_hash = _canonical_hash(
        crosswalk.model_dump(mode="json")
    )
    expected_materialization_hash = _canonical_hash(
        plan.model_dump(mode="json")
    )
    if integration_manifest.get("contract_hash") != (
        manifest.semantic_contract_hash
    ):
        findings.append(
            ArtifactFinding(
                "INTEGRATION_CONTRACT_HASH_DRIFT",
                "semantic-manifest.json contract hash does not match the "
                "sealed model manifest.",
            )
        )
    for field_name, expected_value in (
        ("semantic_model_manifest_hash", manifest.manifest_hash),
        ("semantic_crosswalk_hash", expected_crosswalk_hash),
        ("materialization_plan_hash", expected_materialization_hash),
        ("model_quality_report_hash", quality_report.report_hash),
        ("dependency_graph_hash", dependency_graph.graph_hash),
    ):
        if integration_manifest.get(field_name) != expected_value:
            findings.append(
                ArtifactFinding(
                    "INTEGRATION_HASH_DRIFT",
                    f"semantic-manifest.json field '{field_name}' does not "
                    "match the sealed artifact.",
                )
            )
    if (
        quality_report.report_hash
        != compute_model_quality_report_hash(quality_report)
    ):
        findings.append(
            ArtifactFinding(
                "QUALITY_REPORT_HASH_INVALID",
                "Persisted model quality report hash does not match content.",
            )
        )
    if (
        dependency_graph.graph_hash
        != compute_dependency_graph_hash(dependency_graph)
    ):
        findings.append(
            ArtifactFinding(
                "DEPENDENCY_GRAPH_HASH_INVALID",
                "Persisted dependency graph hash does not match content.",
            )
        )
    if findings:
        raise SemanticCompileError(
            "Persisted semantic authority failed validation: "
            + "; ".join(
                f"{finding.code}: {finding.message}"
                for finding in findings
            )
        )
    return LoadedSemanticModelArtifacts(
        manifest=manifest,
        crosswalk=crosswalk,
        materialization_plan=plan,
        quality_report=quality_report,
        dependency_graph=dependency_graph,
    )
