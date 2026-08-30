"""Cross-artifact validation for one approved semantic contract build."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schemas import (
    PersistedQuerySchema,
    compute_persisted_query_schema_hash,
)


@dataclass(frozen=True)
class ArtifactFinding:
    """One deterministic cross-artifact validation failure."""

    code: str
    message: str


class SemanticArtifactValidationError(ValueError):
    """Raised when compiled surfaces drift from their semantic authority."""

    def __init__(self, findings: list[ArtifactFinding]) -> None:
        self.findings = tuple(findings)
        super().__init__(
            "; ".join(f"{finding.code}: {finding.message}" for finding in findings)
        )


def _load_json(path: Path, findings: list[ArtifactFinding]) -> dict[str, Any]:
    if not path.exists():
        findings.append(ArtifactFinding("ARTIFACT_MISSING", str(path)))
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        findings.append(
            ArtifactFinding("ARTIFACT_INVALID_JSON", f"{path}: {exc}")
        )
        return {}
    if not isinstance(value, dict):
        findings.append(
            ArtifactFinding("ARTIFACT_INVALID_SHAPE", f"{path}: expected object")
        )
        return {}
    return value


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_object_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _verify_manifest_files(
    *,
    root: Path,
    artifacts: Any,
    findings: list[ArtifactFinding],
) -> None:
    if isinstance(artifacts, list):
        entries = {
            str(item.get("path")): item.get("sha256")
            for item in artifacts
            if isinstance(item, dict) and item.get("path")
        }
    elif isinstance(artifacts, dict):
        entries = {str(path): digest for path, digest in artifacts.items()}
    else:
        findings.append(
            ArtifactFinding(
                "MANIFEST_ARTIFACTS_MISSING",
                f"{root}: manifest does not enumerate artifacts",
            )
        )
        return
    for relative_path, expected in entries.items():
        path = root / relative_path
        if not path.exists():
            findings.append(
                ArtifactFinding("MANIFEST_FILE_MISSING", str(path))
            )
            continue
        actual = _sha256(path)
        if expected != actual:
            findings.append(
                ArtifactFinding(
                    "MANIFEST_HASH_MISMATCH",
                    f"{path}: expected {expected}, found {actual}",
                )
            )


def _semantic_ids(items: Any) -> set[str]:
    if not isinstance(items, list):
        return set()
    return {
        str(item["semantic_id"])
        for item in items
        if isinstance(item, dict) and item.get("semantic_id")
    }


def _validate_local_ontology_projection(
    root: Path,
    manifest: Any,
    plan: Any,
    findings: list[ArtifactFinding],
) -> None:
    """Compare locally compiled Ontology parts to the sealed authority."""
    parts: dict[str, dict[str, Any]] = {}
    for entity in manifest.entity_types:
        type_id = entity.ontology_projection.ontology_type_id
        binding_id = entity.ontology_projection.binding_id
        for relative_path in (
            f"EntityTypes/{type_id}/definition.json",
            f"EntityTypes/{type_id}/DataBindings/{binding_id}.json",
        ):
            parts[relative_path] = _load_json(
                root / "ontology" / relative_path,
                findings,
            )
    for relationship in manifest.relationship_types:
        type_id = relationship.ontology_projection.ontology_rel_type_id
        contextualization_id = (
            relationship.ontology_projection.contextualization_id
        )
        for relative_path in (
            f"RelationshipTypes/{type_id}/definition.json",
            (
                f"RelationshipTypes/{type_id}/Contextualizations/"
                f"{contextualization_id}.json"
            ),
        ):
            parts[relative_path] = _load_json(
                root / "ontology" / relative_path,
                findings,
            )
    findings.extend(
        validate_ontology_projection_parts(parts, manifest, plan)
    )


def validate_ontology_projection_parts(
    parts: dict[str, dict[str, Any]],
    manifest: Any,
    plan: Any,
    *,
    workspace_id: str | None = None,
    lakehouse_item_id: str | None = None,
    schema: str | None = None,
) -> list[ArtifactFinding]:
    """Compare decoded Ontology parts to the sealed semantic authority."""
    value_type_map = {
        "string": "String",
        "integer": "BigInt",
        "number": "Double",
        "boolean": "Boolean",
        "datetime": "DateTime",
        "date": "String",
        "uri": "String",
        "json": "String",
    }
    findings: list[ArtifactFinding] = []
    entity_table_by_id = {
        table.semantic_id: table for table in plan.entity_tables
    }
    relationship_table_by_id = {
        table.semantic_id: table for table in plan.relationship_tables
    }
    properties_by_owner: dict[str, list[Any]] = {}
    for prop in manifest.property_definitions:
        properties_by_owner.setdefault(prop.owner_type_id, []).append(prop)
    entity_by_id = {
        entity.semantic_id: entity for entity in manifest.entity_types
    }

    def physical_entity_id_property(
        entity_id: str,
    ) -> Any | None:
        table = entity_table_by_id.get(entity_id)
        if table is None:
            return None
        return next(
            (
                prop
                for prop in properties_by_owner.get(entity_id, [])
                if (
                    prop.physical_source_column or prop.name
                ) == table.entity_id_column
            ),
            None,
        )

    def _source_target_matches(
        source: dict[str, Any],
        *,
        table_name: str,
    ) -> bool:
        return (
            source.get("sourceType") == "LakehouseTable"
            and source.get("sourceTableName") == table_name
            and (schema is None or source.get("sourceSchema") == schema)
            and (
                workspace_id is None
                or source.get("workspaceId") == workspace_id
            )
            and (
                lakehouse_item_id is None
                or source.get("itemId") == lakehouse_item_id
            )
        )

    for entity in manifest.entity_types:
        type_id = entity.ontology_projection.ontology_type_id
        binding_id = entity.ontology_projection.binding_id
        definition_path = f"EntityTypes/{type_id}/definition.json"
        binding_path = (
            f"EntityTypes/{type_id}/DataBindings/{binding_id}.json"
        )
        definition = parts.get(definition_path, {})
        binding = parts.get(binding_path, {})
        if not definition:
            findings.append(ArtifactFinding(
                "ONTOLOGY_ENTITY_DEFINITION_MISSING",
                f"Persisted Ontology omits '{definition_path}'.",
            ))
        if not binding:
            findings.append(ArtifactFinding(
                "ONTOLOGY_ENTITY_BINDING_MISSING",
                f"Persisted Ontology omits '{binding_path}'.",
            ))
        owner_properties = properties_by_owner.get(entity.semantic_id, [])
        expected_properties = {
            prop.name: (
                prop.ontology_projection.ontology_property_id,
                value_type_map[prop.value_type],
            )
            for prop in owner_properties
        }
        actual_properties = {
            str(prop.get("name")): (
                str(prop.get("id")),
                str(prop.get("valueType")),
            )
            for prop in [
                *definition.get("properties", []),
                *definition.get("timeseriesProperties", []),
            ]
            if isinstance(prop, dict) and prop.get("name")
        }
        table = entity_table_by_id.get(entity.semantic_id)
        entity_id_property = physical_entity_id_property(
            entity.semantic_id
        )
        expected_entity_id_parts = (
            [
                entity_id_property.ontology_projection.ontology_property_id
            ]
            if entity_id_property is not None
            else []
        )
        display_property = next(
            (
                prop
                for prop in owner_properties
                if (
                    prop.physical_source_column or prop.name
                )
                == table.display_name_column
            ),
            None,
        ) if table is not None else None
        if (
            definition.get("id") != type_id
            or definition.get("namespace") != "usertypes"
            or definition.get("namespaceType") != "Custom"
            or definition.get("name") != entity.canonical_name
            or definition.get("entityIdParts") != expected_entity_id_parts
            or display_property is None
            or definition.get("displayNamePropertyId")
            != display_property.ontology_projection.ontology_property_id
            or actual_properties != expected_properties
        ):
            findings.append(ArtifactFinding(
                "ONTOLOGY_ENTITY_PROJECTION_DRIFT",
                f"Compiled Ontology entity '{entity.semantic_id}' does not "
                "match the sealed manifest.",
            ))
        binding_configuration = binding.get(
            "dataBindingConfiguration", {}
        )
        source_table = binding_configuration.get(
            "sourceTableProperties", {}
        )
        actual_property_bindings = {
            (
                str(item.get("sourceColumnName")),
                str(item.get("targetPropertyId")),
            )
            for item in binding_configuration.get("propertyBindings", [])
            if isinstance(item, dict)
        }
        expected_property_bindings = {
            (
                str(prop.physical_source_column or prop.name),
                str(prop.ontology_projection.ontology_property_id),
            )
            for prop in properties_by_owner.get(entity.semantic_id, [])
        }
        if table is None or (
            binding.get("id") != binding_id
            or binding_configuration.get("dataBindingType")
            != "NonTimeSeries"
            or not _source_target_matches(
                source_table,
                table_name=table.table_name,
            )
            or actual_property_bindings != expected_property_bindings
        ):
            findings.append(ArtifactFinding(
                "ONTOLOGY_ENTITY_BINDING_DRIFT",
                f"Compiled Ontology binding for '{entity.semantic_id}' does "
                "not match the materialization plan.",
            ))

    for relationship in manifest.relationship_types:
        type_id = relationship.ontology_projection.ontology_rel_type_id
        contextualization_id = (
            relationship.ontology_projection.contextualization_id
        )
        definition_path = f"RelationshipTypes/{type_id}/definition.json"
        binding_path = (
            f"RelationshipTypes/{type_id}/Contextualizations/"
            f"{contextualization_id}.json"
        )
        definition = parts.get(definition_path, {})
        binding = parts.get(binding_path, {})
        if not definition:
            findings.append(ArtifactFinding(
                "ONTOLOGY_RELATIONSHIP_DEFINITION_MISSING",
                f"Persisted Ontology omits '{definition_path}'.",
            ))
        if not binding:
            findings.append(ArtifactFinding(
                "ONTOLOGY_RELATIONSHIP_BINDING_MISSING",
                f"Persisted Ontology omits '{binding_path}'.",
            ))
        table = relationship_table_by_id.get(relationship.semantic_id)
        if (
            definition.get("id") != type_id
            or definition.get("namespace") != "usertypes"
            or definition.get("namespaceType") != "Custom"
            or definition.get("name") != relationship.predicate
            or definition.get("source", {}).get("entityTypeId")
            != relationship.ontology_projection.source_ontology_type_id
            or definition.get("target", {}).get("entityTypeId")
            != relationship.ontology_projection.target_ontology_type_id
        ):
            findings.append(ArtifactFinding(
                "ONTOLOGY_RELATIONSHIP_PROJECTION_DRIFT",
                f"Compiled Ontology relationship "
                f"'{relationship.semantic_id}' does not match the manifest.",
            ))
        source_entity = entity_by_id.get(relationship.source_type_id)
        target_entity = entity_by_id.get(relationship.target_type_id)
        source_id_property = (
            physical_entity_id_property(source_entity.semantic_id)
            if source_entity is not None
            else None
        )
        target_id_property = (
            physical_entity_id_property(target_entity.semantic_id)
            if target_entity is not None
            else None
        )
        expected_source_refs = (
            [{
                "sourceColumnName": table.source_column,
                "targetPropertyId": (
                    source_id_property
                    .ontology_projection
                    .ontology_property_id
                ),
            }]
            if table is not None and source_id_property is not None
            else []
        )
        expected_target_refs = (
            [{
                "sourceColumnName": table.target_column,
                "targetPropertyId": (
                    target_id_property
                    .ontology_projection
                    .ontology_property_id
                ),
            }]
            if table is not None and target_id_property is not None
            else []
        )
        data_binding_table = binding.get("dataBindingTable", {})
        if table is None or (
            binding.get("id") != contextualization_id
            or not _source_target_matches(
                data_binding_table,
                table_name=table.table_name,
            )
            or binding.get("sourceKeyRefBindings")
            != expected_source_refs
            or binding.get("targetKeyRefBindings")
            != expected_target_refs
        ):
            findings.append(ArtifactFinding(
                "ONTOLOGY_RELATIONSHIP_BINDING_DRIFT",
                f"Compiled Ontology contextualization for "
                f"'{relationship.semantic_id}' does not match the plan.",
            ))
    return findings


def _validate_local_graph_projection(
    root: Path,
    manifest: Any,
    plan: Any,
    findings: list[ArtifactFinding],
) -> None:
    """Compare the local Graph definition to manifest labels and tables."""
    graph_definition = _load_json(
        root / "graph" / "graph-definition.json",
        findings,
    )
    if not graph_definition:
        return
    parts = {
        str(part.get("path")): part.get("payload_json")
        for part in graph_definition.get("parts", [])
        if isinstance(part, dict)
        and part.get("path")
        and isinstance(part.get("payload_json"), dict)
    }
    findings.extend(validate_graph_projection_parts(parts, manifest, plan))


def validate_graph_projection_parts(
    parts: dict[str, dict[str, Any]],
    manifest: Any,
    plan: Any,
) -> list[ArtifactFinding]:
    """Compare decoded Graph Model parts to manifest labels and tables."""
    findings: list[ArtifactFinding] = []
    required_parts = {
        "dataSources.json",
        "graphType.json",
        "graphDefinition.json",
    }
    missing_parts = sorted(required_parts - set(parts))
    if missing_parts:
        findings.append(ArtifactFinding(
            "GRAPH_DEFINITION_PARTS_MISSING",
            f"Persisted Graph definition omits required parts: {missing_parts}.",
        ))
        return findings
    data_sources = {
        str(source.get("name")): str(
            source.get("properties", {}).get("path", "")
        ).split("/")[-1]
        for source in parts.get("dataSources.json", {}).get(
            "dataSources",
            [],
        )
        if isinstance(source, dict) and source.get("name")
    }
    graph_type = parts.get("graphType.json", {})
    graph_mapping = parts.get("graphDefinition.json", {})
    node_types = {
        str(node.get("alias")): node
        for node in graph_type.get("nodeTypes", [])
        if isinstance(node, dict) and node.get("alias")
    }
    edge_types = {
        str(edge.get("alias")): edge
        for edge in graph_type.get("edgeTypes", [])
        if isinstance(edge, dict) and edge.get("alias")
    }
    node_tables = {
        str(table.get("nodeTypeAlias")): table
        for table in graph_mapping.get("nodeTables", [])
        if isinstance(table, dict) and table.get("nodeTypeAlias")
    }
    edge_tables = {
        str(table.get("edgeTypeAlias")): table
        for table in graph_mapping.get("edgeTables", [])
        if isinstance(table, dict) and table.get("edgeTypeAlias")
    }
    entity_table_by_id = {
        table.semantic_id: table for table in plan.entity_tables
    }
    relationship_table_by_id = {
        table.semantic_id: table for table in plan.relationship_tables
    }
    entity_alias_by_id = {
        entity.semantic_id: entity.graph_projection.alias
        for entity in manifest.entity_types
    }
    for entity in manifest.entity_types:
        alias = str(entity.graph_projection.alias)
        node_type = node_types.get(alias, {})
        node_table = node_tables.get(alias, {})
        table = entity_table_by_id.get(entity.semantic_id)
        actual_properties = {
            str(prop.get("name"))
            for prop in node_type.get("properties", [])
            if isinstance(prop, dict) and prop.get("name")
        }
        data_source_table = data_sources.get(
            str(node_table.get("dataSourceName"))
        )
        if (
            node_type.get("labels") != [entity.graph_projection.label]
            or actual_properties
            != set(entity.graph_projection.property_keys)
            or table is None
            or data_source_table != table.table_name
        ):
            findings.append(ArtifactFinding(
                "GRAPH_ENTITY_PROJECTION_DRIFT",
                f"Compiled Graph node '{entity.semantic_id}' does not match "
                "the sealed manifest/materialization plan.",
            ))

    for relationship in manifest.relationship_types:
        alias = str(relationship.graph_projection.alias)
        edge_type = edge_types.get(alias, {})
        edge_table = edge_tables.get(alias, {})
        table = relationship_table_by_id.get(relationship.semantic_id)
        data_source_table = data_sources.get(
            str(edge_table.get("dataSourceName"))
        )
        actual_properties = {
            str(prop.get("name"))
            for prop in edge_type.get("properties", [])
            if isinstance(prop, dict) and prop.get("name")
        }
        expected_properties = (
            {column.column_name for column in table.columns}
            if table is not None
            else set()
        )
        if (
            edge_type.get("labels")
            != [relationship.graph_projection.label]
            or edge_type.get("sourceNodeType", {}).get("alias")
            != entity_alias_by_id.get(relationship.source_type_id)
            or edge_type.get("destinationNodeType", {}).get("alias")
            != entity_alias_by_id.get(relationship.target_type_id)
            or actual_properties != expected_properties
            or table is None
            or data_source_table != table.table_name
            or edge_table.get("sourceNodeKeyColumns")
            != [table.source_column]
            or edge_table.get("destinationNodeKeyColumns")
            != [table.target_column]
        ):
            findings.append(ArtifactFinding(
                "GRAPH_RELATIONSHIP_PROJECTION_DRIFT",
                f"Compiled Graph edge '{relationship.semantic_id}' does not "
                "match the sealed manifest/materialization plan.",
            ))
    return findings


def validate_compiled_semantic_artifacts(
    build_dir: Path | str,
    *,
    require_search: bool = True,
    require_competency: bool = False,
    require_model_authority: bool = False,
) -> dict[str, Any]:
    """Validate VAL-052..059 invariants across compiled build surfaces."""
    root = Path(build_dir)
    findings: list[ArtifactFinding] = []

    semantic_manifest = _load_json(
        root / "semantic" / "semantic-manifest.json", findings
    )
    contract = _load_json(
        root / "semantic" / "normalized-contract.json", findings
    )
    ontology_manifest = _load_json(
        root / "ontology" / "ontology-manifest.json", findings
    )
    graph_manifest = _load_json(
        root / "graph" / "graph-manifest.json", findings
    )
    label_catalog = _load_json(
        root / "graph" / "label-catalog.json", findings
    )
    agent_manifest = _load_json(
        root / "agents" / "agent-manifest.json", findings
    )
    agent_context = _load_json(
        root / "agents" / "semantic-context.json", findings
    )
    query_schema: PersistedQuerySchema | None = None
    query_schema_path = root / "agents" / "persisted-query-schema.json"
    if (
        require_model_authority
        or agent_manifest.get("persisted_query_schema_hash")
    ):
        query_schema_raw = _load_json(query_schema_path, findings)
        if query_schema_raw:
            try:
                query_schema = PersistedQuerySchema.model_validate(
                    query_schema_raw
                )
            except ValueError as exc:
                findings.append(ArtifactFinding(
                    "QUERY_SCHEMA_INVALID",
                    f"persisted-query-schema.json is invalid: {exc}",
                ))
            if query_schema is not None:
                expected_query_schema_hash = (
                    compute_persisted_query_schema_hash(query_schema)
                )
                if query_schema.schema_hash != expected_query_schema_hash:
                    findings.append(ArtifactFinding(
                        "QUERY_SCHEMA_HASH_DRIFT",
                        "Persisted query schema hash differs from its contents.",
                    ))
                if (
                    agent_manifest.get("persisted_query_schema_hash")
                    != query_schema.schema_hash
                ):
                    findings.append(ArtifactFinding(
                        "QUERY_SCHEMA_MANIFEST_DRIFT",
                        "Agent manifest query schema hash differs from the "
                        "persisted query schema.",
                    ))
    contract_hash = semantic_manifest.get("contract_hash")
    competency_status = agent_manifest.get("competency_status")
    if competency_status == "compiled":
        competency_path = root / "agents" / "competency-contract.json"
        competency = _load_json(competency_path, findings)
        if competency.get("contract_hash") != contract_hash:
            findings.append(
                ArtifactFinding(
                    "COMPETENCY_CONTRACT_HASH_DRIFT",
                    "Competency contract does not share the semantic contract hash.",
                )
            )
        competency_query_schema = competency.get("query_schema")
        if query_schema is not None and (
            not isinstance(competency_query_schema, dict)
            or competency_query_schema.get("schema_hash")
            != query_schema.schema_hash
        ):
            findings.append(
                ArtifactFinding(
                    "COMPETENCY_QUERY_SCHEMA_DRIFT",
                    "Competency contract does not embed the current persisted "
                    "query schema.",
                )
            )
        if competency_path.exists():
            actual_competency_hash = _sha256(competency_path)
            if (
                agent_manifest.get("competency_contract_hash")
                != actual_competency_hash
            ):
                findings.append(
                    ArtifactFinding(
                        "COMPETENCY_ARTIFACT_HASH_DRIFT",
                        "Competency contract hash differs from the agent manifest.",
                    )
                )
    elif require_competency:
        findings.append(
            ArtifactFinding(
                "COMPETENCY_CONTRACT_INCOMPLETE",
                "No route-aware competency contract was compiled.",
            )
        )

    if not contract_hash:
        findings.append(
            ArtifactFinding(
                "CONTRACT_HASH_MISSING",
                "semantic-manifest.json does not declare contract_hash",
            )
        )
    hash_sources = {
        "ontology": ontology_manifest.get("contract_hash"),
        "graph": graph_manifest.get("contract_hash"),
        "label-catalog": label_catalog.get("contract_hash"),
        "agent": agent_manifest.get("contract_hash"),
        "agent-context": agent_context.get("contract_hash"),
    }
    search_manifest: dict[str, Any] = {}
    if require_search:
        search_manifest = _load_json(
            root / "search" / "search-manifest.json", findings
        )
        hash_sources["search"] = search_manifest.get("contract_hash")
    for surface, value in hash_sources.items():
        if value != contract_hash:
            findings.append(
                ArtifactFinding(
                    "CONTRACT_HASH_DRIFT",
                    f"{surface} declares {value!r}; expected {contract_hash!r}",
                )
            )

    _verify_manifest_files(
        root=root / "semantic",
        artifacts=semantic_manifest.get("artifacts"),
        findings=findings,
    )
    _verify_manifest_files(
        root=root / "ontology",
        artifacts=ontology_manifest.get("artifacts"),
        findings=findings,
    )
    _verify_manifest_files(
        root=root / "graph",
        artifacts=graph_manifest.get("artifacts"),
        findings=findings,
    )

    expected_entity_ids = {
        str(item["id"])
        for item in contract.get("entity_types", [])
        if isinstance(item, dict) and item.get("publication_status") != "excluded"
    }
    expected_relationship_ids = {
        str(item["id"])
        for item in contract.get("relationship_types", [])
        if isinstance(item, dict) and item.get("publication_status") != "excluded"
    }
    catalog_entity_ids = _semantic_ids(label_catalog.get("nodes"))
    catalog_relationship_ids = _semantic_ids(label_catalog.get("edges"))
    context_entity_ids = _semantic_ids(agent_context.get("entity_types"))
    context_relationship_ids = _semantic_ids(
        agent_context.get("relationship_types")
    )
    if catalog_entity_ids != expected_entity_ids:
        findings.append(
            ArtifactFinding(
                "ENTITY_ID_DRIFT",
                "Graph label catalog entity IDs differ from normalized contract.",
            )
        )
    if catalog_relationship_ids != expected_relationship_ids:
        findings.append(
            ArtifactFinding(
                "RELATIONSHIP_ID_DRIFT",
                "Graph label catalog relationship IDs differ from normalized contract.",
            )
        )
    if context_entity_ids != catalog_entity_ids:
        findings.append(
            ArtifactFinding(
                "AGENT_ENTITY_DRIFT",
                "Agent semantic context entity IDs differ from Graph labels.",
            )
        )
    if context_relationship_ids != catalog_relationship_ids:
        findings.append(
            ArtifactFinding(
                "AGENT_RELATIONSHIP_DRIFT",
                "Agent semantic context relationship IDs differ from Graph labels.",
            )
        )

    catalog_edges = {
        str(item.get("semantic_id")): item
        for item in label_catalog.get("edges", [])
        if isinstance(item, dict) and item.get("semantic_id")
    }
    context_edges = {
        str(item.get("semantic_id")): item
        for item in agent_context.get("relationship_types", [])
        if isinstance(item, dict) and item.get("semantic_id")
    }
    comparable_fields = (
        "graph_label",
        "source_graph_label",
        "target_graph_label",
        "direction",
        "evidence_policy",
        "publication_status",
    )
    for semantic_id, catalog_edge in catalog_edges.items():
        context_edge = context_edges.get(semantic_id, {})
        for field in comparable_fields:
            if context_edge.get(field) != catalog_edge.get(field):
                findings.append(
                    ArtifactFinding(
                        "EDGE_DEFINITION_DRIFT",
                        f"{semantic_id}.{field} differs between Graph and agent.",
                    )
                )

    instructions_path = root / "agents" / "instructions.md"
    context_path = root / "agents" / "semantic-context.json"
    if instructions_path.exists():
        actual = _sha256(instructions_path)
        if agent_manifest.get("instruction_hash") != actual:
            findings.append(
                ArtifactFinding(
                    "AGENT_INSTRUCTION_HASH_DRIFT",
                    f"instructions.md expected {agent_manifest.get('instruction_hash')}, "
                    f"found {actual}",
                )
            )
    if context_path.exists():
        actual = _sha256(context_path)
        if agent_manifest.get("semantic_context_hash") != actual:
            findings.append(
                ArtifactFinding(
                    "AGENT_CONTEXT_HASH_DRIFT",
                    f"semantic-context.json expected "
                    f"{agent_manifest.get('semantic_context_hash')}, found {actual}",
                )
            )

    if require_search:
        for index in search_manifest.get("indexes", []):
            if not isinstance(index, dict) or not index.get("name"):
                continue
            index_dir = root / "search" / str(index["name"])
            schema_path = index_dir / "index.schema.json"
            docs_path = index_dir / "docs.json"
            schema = _load_json(schema_path, findings)
            if schema.get("_semantic_contract_hash") != contract_hash:
                findings.append(
                    ArtifactFinding(
                        "SEARCH_SCHEMA_HASH_DRIFT",
                        f"{schema_path} is not bound to {contract_hash}.",
                    )
                )
            field_names = {
                str(field.get("name"))
                for field in schema.get("fields", [])
                if isinstance(field, dict)
            }
            if "semantic_contract_hash" not in field_names:
                findings.append(
                    ArtifactFinding(
                        "SEARCH_HASH_FIELD_MISSING",
                        f"{schema_path} omits semantic_contract_hash.",
                    )
                )
            if index.get("schema_sha256") != _sha256(schema_path):
                findings.append(
                    ArtifactFinding(
                        "SEARCH_SCHEMA_MANIFEST_DRIFT",
                        str(schema_path),
                    )
                )
            if docs_path.exists():
                try:
                    docs = json.loads(docs_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    findings.append(
                        ArtifactFinding(
                            "SEARCH_DOCS_INVALID_JSON", f"{docs_path}: {exc}"
                        )
                    )
                    docs = []
                if not isinstance(docs, list):
                    findings.append(
                        ArtifactFinding(
                            "SEARCH_DOCS_INVALID_SHAPE",
                            f"{docs_path}: expected array",
                        )
                    )
                    docs = []
                for ordinal, document in enumerate(docs):
                    if (
                        not isinstance(document, dict)
                        or document.get("semantic_contract_hash") != contract_hash
                    ):
                        findings.append(
                            ArtifactFinding(
                                "SEARCH_DOCUMENT_HASH_DRIFT",
                                f"{docs_path}[{ordinal}] is not bound to "
                                f"{contract_hash}.",
                            )
                        )
                if index.get("docs_sha256") != _sha256(docs_path):
                    findings.append(
                        ArtifactFinding(
                            "SEARCH_DOCS_MANIFEST_DRIFT",
                            str(docs_path),
                        )
                    )

    if findings:
        raise SemanticArtifactValidationError(findings)

    # ------------------------------------------------------------------
    # SPEC-008A §6/§4.2/§7.2 model-level validation (Blocker 1 wiring)
    # Wire validate_manifest_model_completeness, validate_crosswalk_against_manifest,
    # and validate_materialization_availability into the production path so that
    # these validators are invoked on every real build — not just in tests.
    # Files are optional (not all builds have SPEC-008A artifacts); when present
    # they MUST parse cleanly and pass all model-level checks.
    # ------------------------------------------------------------------
    _spec008a_findings: list[ArtifactFinding] = []
    _parsed_manifest = None
    _parsed_crosswalk = None
    _parsed_plan = None
    _manifest_path = root / "semantic" / "semantic-model-manifest.json"
    _crosswalk_path = root / "semantic" / "semantic-crosswalk.json"
    _matplan_path = root / "semantic" / "materialization-plan.json"
    _quality_path = root / "semantic" / "model-quality-report.json"
    _dependency_path = root / "semantic" / "dependency-graph.json"
    _model_paths = (
        _manifest_path,
        _crosswalk_path,
        _matplan_path,
        _quality_path,
        _dependency_path,
    )
    _present_model_paths = [path for path in _model_paths if path.exists()]
    _complete_model_authority = len(_present_model_paths) == len(_model_paths)
    if require_model_authority and len(_present_model_paths) != len(_model_paths):
        for path in _model_paths:
            if not path.exists():
                _spec008a_findings.append(
                    ArtifactFinding("ARTIFACT_MISSING", str(path))
                )

    if _manifest_path.exists():
        try:
            _manifest_raw = json.loads(_manifest_path.read_text(encoding="utf-8"))
            from pydantic import ValidationError as _ValidationError

            from .schemas import SemanticModelManifest as _SemanticModelManifest

            try:
                _parsed_manifest = _SemanticModelManifest.model_validate(_manifest_raw)
            except _ValidationError as exc:
                _spec008a_findings.append(ArtifactFinding(
                    "SCHEMA_PARSE_ERROR",
                    f"semantic-model-manifest.json failed schema validation: {exc}",
                ))
            if _parsed_manifest is not None:
                _spec008a_findings.extend(
                    validate_manifest_model_completeness(_parsed_manifest)
                )
                if (
                    query_schema is not None
                    and query_schema.manifest_hash
                    != _parsed_manifest.manifest_hash
                ):
                    _spec008a_findings.append(
                        ArtifactFinding(
                            "QUERY_SCHEMA_MODEL_MANIFEST_DRIFT",
                            "Persisted query schema was not derived from the "
                            "current semantic model manifest.",
                        )
                    )
        except (OSError, json.JSONDecodeError) as exc:
            _spec008a_findings.append(ArtifactFinding(
                "SCHEMA_PARSE_ERROR",
                f"semantic-model-manifest.json could not be read: {exc}",
            ))

    if _crosswalk_path.exists():
        try:
            _crosswalk_raw = json.loads(_crosswalk_path.read_text(encoding="utf-8"))
            from pydantic import ValidationError as _ValidationError

            from .schemas import SemanticCrosswalk as _SemanticCrosswalk

            try:
                _parsed_crosswalk = _SemanticCrosswalk.model_validate(_crosswalk_raw)
            except _ValidationError as exc:
                _spec008a_findings.append(ArtifactFinding(
                    "SCHEMA_PARSE_ERROR",
                    f"semantic-crosswalk.json failed schema validation: {exc}",
                ))
            if _parsed_crosswalk is not None and _parsed_manifest is not None:
                _spec008a_findings.extend(
                    validate_crosswalk_against_manifest(
                        _parsed_crosswalk, _parsed_manifest
                    )
                )
        except (OSError, json.JSONDecodeError) as exc:
            _spec008a_findings.append(ArtifactFinding(
                "SCHEMA_PARSE_ERROR",
                f"semantic-crosswalk.json could not be read: {exc}",
            ))

    if _matplan_path.exists():
        try:
            _matplan_raw = json.loads(_matplan_path.read_text(encoding="utf-8"))
            from pydantic import ValidationError as _ValidationError

            from .schemas import MaterializationPlan as _MaterializationPlan

            try:
                _parsed_plan = _MaterializationPlan.model_validate(_matplan_raw)
            except _ValidationError as exc:
                _spec008a_findings.append(ArtifactFinding(
                    "SCHEMA_PARSE_ERROR",
                    f"materialization-plan.json failed schema validation: {exc}",
                ))
            if _parsed_plan is not None and _parsed_manifest is not None:
                _spec008a_findings.extend(
                    validate_materialization_availability(
                        _parsed_plan, _parsed_manifest
                    )
                )
        except (OSError, json.JSONDecodeError) as exc:
            _spec008a_findings.append(ArtifactFinding(
                "SCHEMA_PARSE_ERROR",
                f"materialization-plan.json could not be read: {exc}",
            ))

    _parsed_quality = None
    if _quality_path.exists():
        try:
            from pydantic import ValidationError as _ValidationError

            from .schemas import (
                SemanticModelQualityReport as _SemanticModelQualityReport,
                compute_model_quality_report_hash as _compute_quality_hash,
            )

            try:
                _parsed_quality = _SemanticModelQualityReport.model_validate_json(
                    _quality_path.read_text(encoding="utf-8")
                )
            except _ValidationError as exc:
                _spec008a_findings.append(ArtifactFinding(
                    "SCHEMA_PARSE_ERROR",
                    f"model-quality-report.json failed schema validation: {exc}",
                ))
            if _parsed_quality is not None:
                if (
                    _parsed_quality.report_hash
                    != _compute_quality_hash(_parsed_quality)
                ):
                    _spec008a_findings.append(ArtifactFinding(
                        "MODEL_QUALITY_HASH_INVALID",
                        "model-quality-report.json content does not match report_hash.",
                    ))
                if _parsed_quality.status != "passed":
                    _spec008a_findings.append(ArtifactFinding(
                        "MODEL_QUALITY_FAILED",
                        "The semantic model quality report is not passed.",
                    ))
        except (OSError, json.JSONDecodeError) as exc:
            _spec008a_findings.append(ArtifactFinding(
                "SCHEMA_PARSE_ERROR",
                f"model-quality-report.json could not be read: {exc}",
            ))

    _parsed_dependency = None
    if _dependency_path.exists():
        try:
            from pydantic import ValidationError as _ValidationError

            from .schemas import (
                SemanticDependencyGraph as _SemanticDependencyGraph,
                compute_dependency_graph_hash as _compute_dependency_hash,
            )

            try:
                _parsed_dependency = _SemanticDependencyGraph.model_validate_json(
                    _dependency_path.read_text(encoding="utf-8")
                )
            except _ValidationError as exc:
                _spec008a_findings.append(ArtifactFinding(
                    "SCHEMA_PARSE_ERROR",
                    f"dependency-graph.json failed schema validation: {exc}",
                ))
            if _parsed_dependency is not None and (
                _parsed_dependency.graph_hash
                != _compute_dependency_hash(_parsed_dependency)
            ):
                _spec008a_findings.append(ArtifactFinding(
                    "DEPENDENCY_GRAPH_HASH_INVALID",
                    "dependency-graph.json content does not match graph_hash.",
                ))
        except (OSError, json.JSONDecodeError) as exc:
            _spec008a_findings.append(ArtifactFinding(
                "SCHEMA_PARSE_ERROR",
                f"dependency-graph.json could not be read: {exc}",
            ))

    if _parsed_manifest is not None and _complete_model_authority:
        declared_manifest_hash = _parsed_manifest.manifest_hash
        semantic_artifacts = semantic_manifest.get("artifacts")
        if isinstance(semantic_artifacts, list):
            enumerated_paths = {
                str(item.get("path"))
                for item in semantic_artifacts
                if isinstance(item, dict) and item.get("path")
            }
        elif isinstance(semantic_artifacts, dict):
            enumerated_paths = {
                str(path) for path in semantic_artifacts
            }
        else:
            enumerated_paths = set()
        required_model_paths = {
            "semantic-model-manifest.json",
            "semantic-crosswalk.json",
            "materialization-plan.json",
            "model-quality-report.json",
            "dependency-graph.json",
        }
        missing_enumeration = sorted(
            required_model_paths - enumerated_paths
        )
        if missing_enumeration:
            _spec008a_findings.append(ArtifactFinding(
                "MODEL_AUTHORITY_NOT_ENUMERATED",
                "semantic-manifest.json does not enumerate required model "
                f"artifacts: {missing_enumeration}.",
            ))
        if (
            semantic_manifest.get("semantic_model_manifest_hash")
            != declared_manifest_hash
        ):
            _spec008a_findings.append(ArtifactFinding(
                "SEMANTIC_MODEL_MANIFEST_HASH_DRIFT",
                "semantic-manifest.json does not bind the sealed model manifest.",
            ))
        crosswalk_hash = (
            _canonical_object_hash(
                _parsed_crosswalk.model_dump(mode="json")
            )
            if _parsed_crosswalk is not None
            else None
        )
        materialization_hash = (
            _canonical_object_hash(_parsed_plan.model_dump(mode="json"))
            if _parsed_plan is not None
            else None
        )
        if _parsed_plan is not None:
            for table in [
                *_parsed_plan.entity_tables,
                *_parsed_plan.relationship_tables,
            ]:
                if not table.source_table_name:
                    _spec008a_findings.append(ArtifactFinding(
                        "MATERIALIZATION_SOURCE_MISSING",
                        f"Materialization table '{table.semantic_id}' does not "
                        "declare its canonical source table.",
                    ))
            _validate_local_ontology_projection(
                root,
                _parsed_manifest,
                _parsed_plan,
                _spec008a_findings,
            )
            _validate_local_graph_projection(
                root,
                _parsed_manifest,
                _parsed_plan,
                _spec008a_findings,
            )
        if (
            semantic_manifest.get("semantic_crosswalk_hash")
            != crosswalk_hash
        ):
            _spec008a_findings.append(ArtifactFinding(
                "SEMANTIC_CROSSWALK_HASH_DRIFT",
                "semantic-manifest.json does not bind the persisted crosswalk.",
            ))
        if (
            semantic_manifest.get("materialization_plan_hash")
            != materialization_hash
        ):
            _spec008a_findings.append(ArtifactFinding(
                "MATERIALIZATION_PLAN_HASH_DRIFT",
                "semantic-manifest.json does not bind the materialization plan.",
            ))
        if (
            _parsed_quality is not None
            and semantic_manifest.get("model_quality_report_hash")
            != _parsed_quality.report_hash
        ):
            _spec008a_findings.append(ArtifactFinding(
                "MODEL_QUALITY_REPORT_HASH_DRIFT",
                "semantic-manifest.json does not bind the model quality report.",
            ))
        if (
            _parsed_dependency is not None
            and semantic_manifest.get("dependency_graph_hash")
            != _parsed_dependency.graph_hash
        ):
            _spec008a_findings.append(ArtifactFinding(
                "DEPENDENCY_GRAPH_MANIFEST_HASH_DRIFT",
                "semantic-manifest.json does not bind the dependency graph.",
            ))
        for surface_name, payload in (
            ("ontology", ontology_manifest),
            ("graph", graph_manifest),
            ("agent", agent_manifest),
        ):
            if (
                payload.get("semantic_model_manifest_hash")
                != declared_manifest_hash
            ):
                _spec008a_findings.append(ArtifactFinding(
                    "PROJECTION_MANIFEST_HASH_DRIFT",
                    f"{surface_name} is not bound to the sealed model manifest.",
                ))
        for surface_name, payload in (
            ("ontology", ontology_manifest),
            ("graph", graph_manifest),
            ("agent", agent_manifest),
        ):
            if (
                payload.get("semantic_crosswalk_hash")
                != crosswalk_hash
            ):
                _spec008a_findings.append(ArtifactFinding(
                    "PROJECTION_CROSSWALK_HASH_DRIFT",
                    f"{surface_name} is not bound to the canonical crosswalk.",
                ))
        for surface_name, payload in (
            ("ontology", ontology_manifest),
            ("graph", graph_manifest),
        ):
            if (
                payload.get("materialization_plan_hash")
                != materialization_hash
            ):
                _spec008a_findings.append(ArtifactFinding(
                    "PROJECTION_MATERIALIZATION_HASH_DRIFT",
                    f"{surface_name} is not bound to the materialization plan.",
                ))
        if require_search and (
            search_manifest.get("semantic_model_manifest_hash")
            != declared_manifest_hash
        ):
            _spec008a_findings.append(ArtifactFinding(
                "PROJECTION_MANIFEST_HASH_DRIFT",
                "search is not bound to the sealed model manifest.",
            ))
        if require_search and (
            search_manifest.get("semantic_crosswalk_hash")
            != crosswalk_hash
        ):
            _spec008a_findings.append(ArtifactFinding(
                "PROJECTION_CROSSWALK_HASH_DRIFT",
                "search is not bound to the canonical crosswalk.",
            ))
        manifest_entity_ids = {
            entity.semantic_id for entity in _parsed_manifest.entity_types
        }
        manifest_relationship_ids = {
            relationship.semantic_id
            for relationship in _parsed_manifest.relationship_types
        }
        if catalog_entity_ids != manifest_entity_ids:
            _spec008a_findings.append(ArtifactFinding(
                "MANIFEST_GRAPH_ENTITY_DRIFT",
                "Graph label catalog differs from the sealed manifest entity set.",
            ))
        if catalog_relationship_ids != manifest_relationship_ids:
            _spec008a_findings.append(ArtifactFinding(
                "MANIFEST_GRAPH_RELATIONSHIP_DRIFT",
                "Graph label catalog differs from the sealed manifest relationship set.",
            ))
        context_property_keys = {
            (
                str(item.get("owner_type_id")),
                str(item.get("semantic_id")),
            )
            for item in agent_context.get("property_definitions", [])
            if isinstance(item, dict)
            and item.get("owner_type_id")
            and item.get("semantic_id")
        }
        manifest_property_keys = {
            (prop.owner_type_id, prop.property_id)
            for prop in _parsed_manifest.property_definitions
            if prop.agent_visible
        }
        if not manifest_property_keys.issubset(context_property_keys):
            _spec008a_findings.append(ArtifactFinding(
                "AGENT_PROPERTY_PROJECTION_DRIFT",
                "Agent semantic context omits manifest agent-visible properties.",
            ))
        if (
            _parsed_quality is not None
            and _parsed_quality.manifest_hash != declared_manifest_hash
        ):
            _spec008a_findings.append(ArtifactFinding(
                "MODEL_QUALITY_MANIFEST_HASH_DRIFT",
                "Model quality report references a different manifest.",
            ))
        if (
            _parsed_dependency is not None
            and _parsed_dependency.manifest_hash != declared_manifest_hash
        ):
            _spec008a_findings.append(ArtifactFinding(
                "DEPENDENCY_MANIFEST_HASH_DRIFT",
                "Dependency graph references a different manifest.",
            ))

    if _spec008a_findings:
        raise SemanticArtifactValidationError(_spec008a_findings)

    return {
        "status": "passed",
        "contract_hash": contract_hash,
        "semantic_model_manifest_hash": (
            _parsed_manifest.manifest_hash
            if _parsed_manifest is not None
            else None
        ),
        "validated_surfaces": sorted(hash_sources),
        "entity_type_count": len(expected_entity_ids),
        "relationship_type_count": len(expected_relationship_ids),
    }

import re as _re

_PLACEHOLDER_RE = _re.compile(
    r"^(?:todo|placeholder|tbd|n/a|an? \w+\.|description\.|"
    r"no description|undefined|unknown|fill in later|fixme)$",
    _re.IGNORECASE,
)


def validate_manifest_model_completeness(
    manifest: Any,
    crosswalk: Any | None = None,
) -> list[ArtifactFinding]:
    """Validate SPEC-008A §6 completeness invariants against in-memory model objects.

    Checks agent-visible property children, relationship physical endpoints,
    placeholder descriptions, property owner membership, and (optionally)
    name-only crosswalk alignment.

    Args:
        manifest: A SemanticModelManifest instance.
        crosswalk: Optional SemanticCrosswalk instance for alignment checks.

    Returns:
        List of ArtifactFinding.  Empty when all invariants pass.
    """
    findings: list[ArtifactFinding] = []
    entity_by_id = {
        entity.semantic_id: entity
        for entity in getattr(manifest, "entity_types", [])
    }

    # §4.2.1: agent-visible properties must have agent_projection.child_id
    for prop in getattr(manifest, "property_definitions", []):
        if getattr(prop, "agent_visible", False):
            agent_proj = getattr(prop, "agent_projection", None)
            child_id = getattr(agent_proj, "child_id", None) if agent_proj else None
            if not child_id:
                findings.append(ArtifactFinding(
                    "MANIFEST_AGENT_CHILD_MISSING",
                    f"Property '{prop.property_id}' (owner: '{prop.owner_type_id}') "
                    "is agent_visible=True but agent_projection.child_id is absent. "
                    "Agent property-child projection is incomplete.",
                ))

    # §4.2.2: relationship types must have Graph endpoint labels and Ontology type IDs
    for rel in getattr(manifest, "relationship_types", []):
        source_entity = entity_by_id.get(rel.source_type_id)
        target_entity = entity_by_id.get(rel.target_type_id)
        gp = getattr(rel, "graph_projection", None)
        if gp is not None:
            src_lbl = getattr(gp, "source_label", None)
            tgt_lbl = getattr(gp, "target_label", None)
            if src_lbl is None or tgt_lbl is None:
                findings.append(ArtifactFinding(
                    "MANIFEST_REL_GRAPH_ENDPOINTS_MISSING",
                    f"Relationship '{rel.semantic_id}' (predicate: '{rel.predicate}') "
                    "is missing Graph endpoint labels "
                    f"(source_label={src_lbl!r}, target_label={tgt_lbl!r}). "
                    "Physical Graph compilation cannot determine endpoints.",
                ))
            elif source_entity is not None and target_entity is not None:
                expected_source_label = getattr(
                    getattr(source_entity, "graph_projection", None),
                    "label",
                    None,
                )
                expected_target_label = getattr(
                    getattr(target_entity, "graph_projection", None),
                    "label",
                    None,
                )
                if (
                    src_lbl != expected_source_label
                    or tgt_lbl != expected_target_label
                ):
                    findings.append(ArtifactFinding(
                        "MANIFEST_REL_GRAPH_ENDPOINTS_MISMATCH",
                        f"Relationship '{rel.semantic_id}' Graph endpoints "
                        f"({src_lbl!r}, {tgt_lbl!r}) do not match entity "
                        f"projections ({expected_source_label!r}, "
                        f"{expected_target_label!r}).",
                    ))
        op = getattr(rel, "ontology_projection", None)
        if op is not None:
            src_oid = getattr(op, "source_ontology_type_id", None)
            tgt_oid = getattr(op, "target_ontology_type_id", None)
            if src_oid is None or tgt_oid is None:
                findings.append(ArtifactFinding(
                    "MANIFEST_REL_ONTOLOGY_ENDPOINTS_MISSING",
                    f"Relationship '{rel.semantic_id}' (predicate: '{rel.predicate}') "
                    "is missing Ontology endpoint type IDs "
                    f"(source={src_oid!r}, target={tgt_oid!r}).",
                ))
            elif source_entity is not None and target_entity is not None:
                expected_source_oid = getattr(
                    getattr(source_entity, "ontology_projection", None),
                    "ontology_type_id",
                    None,
                )
                expected_target_oid = getattr(
                    getattr(target_entity, "ontology_projection", None),
                    "ontology_type_id",
                    None,
                )
                if (
                    src_oid != expected_source_oid
                    or tgt_oid != expected_target_oid
                ):
                    findings.append(ArtifactFinding(
                        "MANIFEST_REL_ONTOLOGY_ENDPOINTS_MISMATCH",
                        f"Relationship '{rel.semantic_id}' Ontology endpoints "
                        f"({src_oid!r}, {tgt_oid!r}) do not match entity "
                        f"projections ({expected_source_oid!r}, "
                        f"{expected_target_oid!r}).",
                    ))

    # §5.7: placeholder descriptions for published entity and relationship types
    for etype in getattr(manifest, "entity_types", []):
        desc = getattr(etype, "description", "") or ""
        if _PLACEHOLDER_RE.fullmatch(desc.strip()):
            findings.append(ArtifactFinding(
                "MANIFEST_PLACEHOLDER_DESCRIPTION",
                f"Entity type '{etype.semantic_id}' has a placeholder description: "
                f"{desc!r}. Use a meaningful business description.",
            ))
    for rel in getattr(manifest, "relationship_types", []):
        desc = getattr(rel, "description", "") or ""
        if _PLACEHOLDER_RE.fullmatch(desc.strip()):
            findings.append(ArtifactFinding(
                "MANIFEST_PLACEHOLDER_DESCRIPTION",
                f"Relationship type '{rel.semantic_id}' has a placeholder description: "
                f"{desc!r}. Use a meaningful business description.",
            ))

    # Property owners must reference known entity types
    known_entity_ids = set(entity_by_id)
    for prop in getattr(manifest, "property_definitions", []):
        if known_entity_ids and prop.owner_type_id not in known_entity_ids:
            findings.append(ArtifactFinding(
                "MANIFEST_PROPERTY_UNKNOWN_OWNER",
                f"Property '{prop.property_id}' references unknown owner "
                f"'{prop.owner_type_id}'. Owner must be a published entity type.",
            ))

    # Optional: name-only crosswalk alignment (§1.2)
    if crosswalk is not None:
        findings.extend(
            validate_crosswalk_against_manifest(crosswalk, manifest)
        )

    return findings


def validate_crosswalk_against_manifest(
    crosswalk: Any,
    manifest: Any,
) -> list[ArtifactFinding]:
    """Validate crosswalk entries against the semantic model manifest (SPEC-008A §4.2).

    Checks:
    - name-only alignment: graph_label matches canonical name without physical ID
    - all crosswalk semantic IDs reference known manifest elements
    - element_kind consistency (shape-level; also enforced by SemanticCrosswalk schema)

    Args:
        crosswalk: A SemanticCrosswalk instance.
        manifest: A SemanticModelManifest instance.

    Returns:
        List of ArtifactFinding.  Empty when all invariants pass.
    """
    findings: list[ArtifactFinding] = []

    canonical_names_lower = {
        e.canonical_name.lower()
        for e in getattr(manifest, "entity_types", [])
    }
    entity_by_id = {
        e.semantic_id: e
        for e in getattr(manifest, "entity_types", [])
        if getattr(e, "publication_status", None) != "excluded"
    }
    rel_by_id = {
        r.semantic_id: r
        for r in getattr(manifest, "relationship_types", [])
        if getattr(r, "publication_status", None) != "excluded"
    }
    prop_by_key = {
        (p.owner_type_id, p.property_id): p
        for p in getattr(manifest, "property_definitions", [])
    }
    entity_ids = set(entity_by_id)
    rel_ids = set(rel_by_id)
    prop_keys = set(prop_by_key)

    manifest_hash = getattr(manifest, "manifest_hash", "")
    crosswalk_hash = getattr(crosswalk, "manifest_hash", "")
    if manifest_hash and crosswalk_hash != manifest_hash:
        findings.append(ArtifactFinding(
            "CROSSWALK_MANIFEST_HASH_MISMATCH",
            "SemanticCrosswalk.manifest_hash does not match the sealed "
            "SemanticModelManifest.manifest_hash.",
        ))

    entity_entries = {
        entry.semantic_id: entry
        for entry in getattr(crosswalk, "entity_type_entries", [])
    }
    relationship_entries = {
        entry.semantic_id: entry
        for entry in getattr(crosswalk, "relationship_type_entries", [])
    }
    property_entries = {
        (getattr(entry, "owner_type_id", None), entry.semantic_id): entry
        for entry in getattr(crosswalk, "property_entries", [])
    }
    publication_profile = getattr(manifest, "publication_profile", None)

    def _compare_mapping(
        *,
        semantic_id: str,
        target: str,
        actual: Any,
        expected: Any,
        enabled: bool,
    ) -> None:
        if not enabled:
            return
        if expected in {None, ""}:
            findings.append(ArtifactFinding(
                "CROSSWALK_MANIFEST_PROJECTION_MISSING",
                f"Manifest projection for '{semantic_id}' has no {target} "
                "physical identifier.",
            ))
            return
        if actual != expected:
            findings.append(ArtifactFinding(
                "CROSSWALK_PHYSICAL_MAPPING_MISMATCH",
                f"Crosswalk {target} mapping for '{semantic_id}' is "
                f"{actual!r}; expected {expected!r} from the manifest.",
            ))

    for semantic_id in sorted(entity_ids - set(entity_entries)):
        findings.append(ArtifactFinding(
            "CROSSWALK_ENTITY_MISSING",
            f"Published entity type '{semantic_id}' has no crosswalk entry.",
        ))
    for semantic_id in sorted(rel_ids - set(relationship_entries)):
        findings.append(ArtifactFinding(
            "CROSSWALK_RELATIONSHIP_MISSING",
            f"Published relationship type '{semantic_id}' has no crosswalk entry.",
        ))
    for owner_type_id, property_id in sorted(prop_keys - set(property_entries)):
        findings.append(ArtifactFinding(
            "CROSSWALK_PROPERTY_MISSING",
            f"Property '{owner_type_id}/{property_id}' has no owner-scoped "
            "crosswalk entry.",
        ))

    for entry in getattr(crosswalk, "entity_type_entries", []):
        # Name-only alignment check
        graph_lbl = getattr(entry, "graph_label", None)
        ont_id = getattr(entry, "ontology_type_id", None)
        agent_id = getattr(entry, "data_agent_element_id", None)
        if (
            graph_lbl is not None
            and graph_lbl.lower() in canonical_names_lower
            and not ont_id
            and not agent_id
        ):
            findings.append(ArtifactFinding(
                "CROSSWALK_NAME_ONLY_ALIGNMENT",
                f"CrosswalkEntry '{entry.semantic_id}': graph_label "
                f"'{graph_lbl}' matches a canonical name but "
                "ontology_type_id and data_agent_element_id are both absent. "
                "Name-only alignment without machine-verifiable canonical ID evidence "
                "(SPEC-008A §1.2).",
            ))
        # Manifest membership
        if entry.semantic_id not in entity_ids:
            findings.append(ArtifactFinding(
                "CROSSWALK_UNKNOWN_ENTITY",
                f"CrosswalkEntry '{entry.semantic_id}' references an entity type "
                "not present in the manifest.",
            ))
            continue
        manifest_entity = entity_by_id[entry.semantic_id]
        _compare_mapping(
            semantic_id=entry.semantic_id,
            target="ontology_type_id",
            actual=getattr(entry, "ontology_type_id", None),
            expected=getattr(
                getattr(manifest_entity, "ontology_projection", None),
                "ontology_type_id",
                None,
            ),
            enabled=bool(
                getattr(publication_profile, "ontology_enabled", True)
            ),
        )
        _compare_mapping(
            semantic_id=entry.semantic_id,
            target="graph_label",
            actual=getattr(entry, "graph_label", None),
            expected=getattr(
                getattr(manifest_entity, "graph_projection", None),
                "label",
                None,
            ),
            enabled=bool(getattr(publication_profile, "graph_enabled", True)),
        )
        expected_graph_alias = getattr(
            getattr(manifest_entity, "graph_projection", None),
            "alias",
            None,
        )
        if expected_graph_alias is not None or entry.graph_alias is not None:
            _compare_mapping(
                semantic_id=entry.semantic_id,
                target="graph_alias",
                actual=getattr(entry, "graph_alias", None),
                expected=expected_graph_alias,
                enabled=bool(
                    getattr(publication_profile, "graph_enabled", True)
                ),
            )
        _compare_mapping(
            semantic_id=entry.semantic_id,
            target="data_agent_element_id",
            actual=getattr(entry, "data_agent_element_id", None),
            expected=getattr(
                getattr(manifest_entity, "agent_projection", None),
                "element_id",
                None,
            ),
            enabled=bool(getattr(publication_profile, "agent_enabled", True)),
        )
        _compare_mapping(
            semantic_id=entry.semantic_id,
            target="search_field_or_filter",
            actual=getattr(entry, "search_field_or_filter", None),
            expected=getattr(
                getattr(manifest_entity, "search_linkage", None),
                "type_filter_field",
                None,
            ),
            enabled=bool(getattr(publication_profile, "search_enabled", True)),
        )
        expected_table = getattr(manifest_entity, "physical_source_table", None)
        if expected_table is not None and entry.physical_table != expected_table:
            findings.append(ArtifactFinding(
                "CROSSWALK_PHYSICAL_MAPPING_MISMATCH",
                f"Crosswalk physical_table for '{entry.semantic_id}' is "
                f"{entry.physical_table!r}; expected {expected_table!r}.",
            ))

    for entry in getattr(crosswalk, "relationship_type_entries", []):
        if entry.semantic_id not in rel_ids:
            findings.append(ArtifactFinding(
                "CROSSWALK_UNKNOWN_RELATIONSHIP",
                f"CrosswalkEntry '{entry.semantic_id}' references a relationship type "
                "not present in the manifest.",
            ))
            continue
        manifest_rel = rel_by_id[entry.semantic_id]
        if (
            getattr(entry, "source_type_id", None) != manifest_rel.source_type_id
            or getattr(entry, "target_type_id", None) != manifest_rel.target_type_id
            or getattr(entry, "direction", None) != manifest_rel.direction
        ):
            findings.append(ArtifactFinding(
                "CROSSWALK_RELATIONSHIP_ENDPOINT_MISMATCH",
                f"Crosswalk relationship '{entry.semantic_id}' endpoint or "
                "direction metadata does not match the manifest.",
            ))
        _compare_mapping(
            semantic_id=entry.semantic_id,
            target="ontology_type_id",
            actual=getattr(entry, "ontology_type_id", None),
            expected=getattr(
                getattr(manifest_rel, "ontology_projection", None),
                "ontology_rel_type_id",
                None,
            ),
            enabled=bool(
                getattr(publication_profile, "ontology_enabled", True)
            ),
        )
        _compare_mapping(
            semantic_id=entry.semantic_id,
            target="graph_label",
            actual=getattr(entry, "graph_label", None),
            expected=getattr(
                getattr(manifest_rel, "graph_projection", None),
                "label",
                None,
            ),
            enabled=bool(getattr(publication_profile, "graph_enabled", True)),
        )
        expected_graph_alias = getattr(
            getattr(manifest_rel, "graph_projection", None),
            "alias",
            None,
        )
        if expected_graph_alias is not None or entry.graph_alias is not None:
            _compare_mapping(
                semantic_id=entry.semantic_id,
                target="graph_alias",
                actual=getattr(entry, "graph_alias", None),
                expected=expected_graph_alias,
                enabled=bool(
                    getattr(publication_profile, "graph_enabled", True)
                ),
            )
        _compare_mapping(
            semantic_id=entry.semantic_id,
            target="data_agent_element_id",
            actual=getattr(entry, "data_agent_element_id", None),
            expected=getattr(
                getattr(manifest_rel, "agent_projection", None),
                "element_id",
                None,
            ),
            enabled=bool(getattr(publication_profile, "agent_enabled", True)),
        )
        expected_table = getattr(manifest_rel, "physical_source_table", None)
        if expected_table is not None and entry.physical_table != expected_table:
            findings.append(ArtifactFinding(
                "CROSSWALK_PHYSICAL_MAPPING_MISMATCH",
                f"Crosswalk physical_table for '{entry.semantic_id}' is "
                f"{entry.physical_table!r}; expected {expected_table!r}.",
            ))

    for entry in getattr(crosswalk, "property_entries", []):
        property_key = (getattr(entry, "owner_type_id", None), entry.semantic_id)
        if property_key not in prop_keys:
            findings.append(ArtifactFinding(
                "CROSSWALK_UNKNOWN_PROPERTY",
                f"CrosswalkEntry '{property_key[0]}/{property_key[1]}' "
                "references an owner-scoped property not present in the manifest.",
            ))
            continue
        manifest_property = prop_by_key[property_key]
        scoped_id = f"{property_key[0]}/{property_key[1]}"
        _compare_mapping(
            semantic_id=scoped_id,
            target="ontology_type_id",
            actual=getattr(entry, "ontology_type_id", None),
            expected=getattr(
                getattr(manifest_property, "ontology_projection", None),
                "ontology_property_id",
                None,
            ),
            enabled=bool(
                getattr(publication_profile, "ontology_enabled", True)
            ),
        )
        _compare_mapping(
            semantic_id=scoped_id,
            target="graph_label",
            actual=getattr(entry, "graph_label", None),
            expected=getattr(
                getattr(manifest_property, "graph_projection", None),
                "property_key",
                None,
            ),
            enabled=bool(getattr(publication_profile, "graph_enabled", True)),
        )
        _compare_mapping(
            semantic_id=scoped_id,
            target="data_agent_element_id",
            actual=getattr(entry, "data_agent_element_id", None),
            expected=getattr(
                getattr(manifest_property, "agent_projection", None),
                "child_id",
                None,
            ),
            enabled=bool(
                getattr(publication_profile, "agent_enabled", True)
                and getattr(manifest_property, "agent_visible", False)
            ),
        )
        expected_search_field = (
            getattr(manifest_property, "physical_source_column", None)
            or manifest_property.property_id
        )
        _compare_mapping(
            semantic_id=scoped_id,
            target="search_field_or_filter",
            actual=getattr(entry, "search_field_or_filter", None),
            expected=expected_search_field,
            enabled=bool(getattr(publication_profile, "search_enabled", True)),
        )
        owner = entity_by_id.get(manifest_property.owner_type_id)
        expected_table = getattr(owner, "physical_source_table", None)
        if expected_table is not None and entry.physical_table != expected_table:
            findings.append(ArtifactFinding(
                "CROSSWALK_PHYSICAL_MAPPING_MISMATCH",
                f"Crosswalk physical_table for '{scoped_id}' is "
                f"{entry.physical_table!r}; expected {expected_table!r}.",
            ))

    return findings


def validate_materialization_availability(
    plan: Any,
    manifest: Any,
) -> list[ArtifactFinding]:
    """Validate materialization plan membership and status consistency (SPEC-008A §7.2).

    Checks:
    - entity and relationship table semantic IDs are in the manifest
    - DataAvailability entries reference known manifest elements
    - status/count consistency (also enforced by DataAvailability schema)
    - authoritative definitions are preserved independently of observations

    Args:
        plan: A MaterializationPlan instance.
        manifest: A SemanticModelManifest instance.

    Returns:
        List of ArtifactFinding.  Empty when all invariants pass.
    """
    findings: list[ArtifactFinding] = []
    from .schemas import compute_manifest_hash

    declared_manifest_hash = getattr(manifest, "manifest_hash", "")
    computed_manifest_hash = compute_manifest_hash(manifest)
    if not declared_manifest_hash:
        findings.append(ArtifactFinding(
            "MANIFEST_HASH_MISSING",
            "Materialization validation requires a sealed semantic manifest.",
        ))
    elif declared_manifest_hash != computed_manifest_hash:
        findings.append(ArtifactFinding(
            "MANIFEST_HASH_INVALID",
            "SemanticModelManifest.manifest_hash does not match its canonical "
            "content hash.",
        ))

    plan_manifest_hash = getattr(plan, "manifest_hash", "")
    if not plan_manifest_hash:
        findings.append(ArtifactFinding(
            "PLAN_MANIFEST_HASH_MISSING",
            "MaterializationPlan.manifest_hash is required.",
        ))
    elif plan_manifest_hash != declared_manifest_hash:
        findings.append(ArtifactFinding(
            "PLAN_MANIFEST_HASH_MISMATCH",
            "MaterializationPlan.manifest_hash does not match the sealed "
            "SemanticModelManifest.",
        ))

    entity_ids = {e.semantic_id for e in getattr(manifest, "entity_types", [])}
    rel_ids = {r.semantic_id for r in getattr(manifest, "relationship_types", [])}
    known_ids = entity_ids | rel_ids
    table_ids = {
        table.semantic_id for table in getattr(plan, "entity_tables", [])
    } | {
        table.semantic_id for table in getattr(plan, "relationship_tables", [])
    }
    availability_by_id = {
        avail.semantic_id: avail
        for avail in getattr(plan, "data_availability", [])
    }
    for semantic_id in sorted(table_ids - set(availability_by_id)):
        findings.append(ArtifactFinding(
            "PLAN_AVAILABILITY_MISSING",
            f"Materialized semantic type '{semantic_id}' has no "
            "DataAvailability record.",
        ))
    for semantic_id in sorted(set(availability_by_id) - table_ids):
        findings.append(ArtifactFinding(
            "PLAN_AVAILABILITY_WITHOUT_TABLE",
            f"DataAvailability entry '{semantic_id}' has no materialized table.",
        ))

    for table in getattr(plan, "entity_tables", []):
        if table.semantic_id not in entity_ids:
            findings.append(ArtifactFinding(
                "PLAN_ENTITY_TABLE_UNKNOWN",
                f"MaterializationPlan entity_table '{table.semantic_id}' references "
                "an entity type not in the manifest. Authoritative definitions must "
                "not be created outside the approved manifest.",
            ))

    for table in getattr(plan, "relationship_tables", []):
        if table.semantic_id not in rel_ids:
            findings.append(ArtifactFinding(
                "PLAN_RELATIONSHIP_TABLE_UNKNOWN",
                f"MaterializationPlan relationship_table '{table.semantic_id}' "
                "references a relationship type not in the manifest.",
            ))

    for avail in getattr(plan, "data_availability", []):
        if avail.semantic_id not in known_ids:
            findings.append(ArtifactFinding(
                "PLAN_AVAILABILITY_UNKNOWN_ID",
                f"DataAvailability entry '{avail.semantic_id}' references an ID "
                "not present in the manifest entity or relationship types.",
            ))
        # Row-count observations must not suppress required approved definitions
        status = getattr(avail, "status", None)
        observed = getattr(avail, "observed_rows", None)
        required = getattr(avail, "required_rows", 0)
        if status == "sufficient" and (
            observed is None or observed < required
        ):
            findings.append(ArtifactFinding(
                "PLAN_AVAILABILITY_STATUS_CONTRADICTION",
                f"DataAvailability '{avail.semantic_id}' is sufficient but "
                f"observed_rows={observed!r}, required_rows={required}.",
            ))
        if status == "insufficient" and (
            observed is None or observed >= required
        ):
            findings.append(ArtifactFinding(
                "PLAN_AVAILABILITY_STATUS_CONTRADICTION",
                f"DataAvailability '{avail.semantic_id}' is insufficient but "
                f"observed_rows={observed!r}, required_rows={required}.",
            ))
        if status in {"unavailable", "not_observed"} and observed is not None:
            findings.append(ArtifactFinding(
                "PLAN_AVAILABILITY_STATUS_CONTRADICTION",
                f"DataAvailability '{avail.semantic_id}' has status='{status}' "
                f"but observed_rows={observed}.",
            ))
        if (
            status in {"unavailable", "insufficient"}
            and avail.semantic_id in known_ids
        ):
            for table in (
                list(getattr(plan, "entity_tables", []))
                + list(getattr(plan, "relationship_tables", []))
            ):
                if table.semantic_id == avail.semantic_id and getattr(table, "required", False):
                    findings.append(ArtifactFinding(
                        "PLAN_REQUIRED_DEFINITION_UNAVAILABLE",
                        f"Required table '{avail.semantic_id}' has "
                        f"DataAvailability status='{status}'. Required approved "
                        "Ontology/Graph schema must not be silently removed by "
                        "row-count observations (SPEC-008A §7.2).",
                    ))

    return findings
