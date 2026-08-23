"""Persisted Data Agent grounding and exact publication validation."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from fabric_kg_builder.semantic.schemas import (
    AgentPublicationReceipt,
    AgentSelectedSource,
    CompetencyExampleReceipt,
    MaterializationPlan,
    PersistedProjectionReceipt,
    SemanticCrosswalk,
    SemanticModelManifest,
)

from .data_agent import (
    ELEMENT_TYPE_EDGE,
    ELEMENT_TYPE_NODE,
    ELEMENT_TYPE_PROPERTY,
    DataAgentPublishResult,
    DataAgentDefinitionError,
    DataAgentLroFailedError,
    DataAgentSpec,
    DataAgentStageSnapshot,
    DataAgentTargetError,
    DataAgentUpsertResult,
    DataSourceElement,
    FabricDataAgentClient,
    LROTimeoutError,
    stage_snapshot_from_spec,
)
from .transport import HttpError


class AgentPublicationError(RuntimeError):
    """Raised when exact-target or persisted publication evidence drifts."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class PersistedAgentGrounding:
    """Manifest-owned source elements bound to one persisted projection."""

    elements: tuple[DataSourceElement, ...]
    sidecar: dict[str, Any]
    sidecar_hash: str
    selected_element_hash: str
    property_child_coverage: float
    expected_property_child_count: int


def build_public_graph_source_projection(
    grounding: PersistedAgentGrounding,
) -> tuple[tuple[DataSourceElement, ...], dict[str, str]]:
    """Project semantic grounding into the Graph elements Fabric accepts.

    Property children are preserved in the Fabric-supported
    ``graph.property`` shape so that the published definition carries the
    full agent-visible property selection.  Un-stripping here ensures
    ``stage_snapshot_from_spec`` yields a nonzero ``property_child_count``
    and the three-way property comparison in
    ``build_agent_publication_receipt`` is meaningful (fix for #14).
    """
    elements = tuple(
        element
        for element in grounding.elements
    )
    metadata = {
        "fabricKgAgentSchemaHash": grounding.sidecar_hash,
        "fabricKgSemanticModelManifestHash": str(
            grounding.sidecar.get("semantic_model_manifest_hash") or ""
        ),
        "fabricKgPersistedProjectionReceiptHash": str(
            grounding.sidecar.get("persisted_projection_receipt_hash") or ""
        ),
        "fabricKgOntologyItemId": str(
            grounding.sidecar.get("ontology_item_id") or ""
        ),
        "fabricKgGraphModelId": str(
            grounding.sidecar.get("graph_model_id") or ""
        ),
        "fabricKgPropertyChildCoverage": (
            f"{grounding.property_child_coverage:.6f}"
        ),
        "fabricKgExpectedPropertyCount": str(
            grounding.expected_property_child_count
        ),
    }
    return elements, metadata


def build_public_ontology_source_projection(
    grounding: PersistedAgentGrounding,
) -> tuple[tuple[DataSourceElement, ...], dict[str, str]]:
    """Project persisted semantics into the Ontology elements Fabric executes."""
    elements = tuple(
        DataSourceElement(
            id=element.display_name,
            display_name=element.display_name,
            type="ontology.entity",
            is_selected=True,
            description=element.description,
        )
        for element in grounding.elements
        if element.type == ELEMENT_TYPE_NODE
    )
    if not elements:
        raise AgentPublicationError(
            "AGENT_EMPTY_SELECTION",
            "Required Ontology source has no selected semantic entities.",
        )
    metadata = {
        "fabricKgAgentSchemaHash": grounding.sidecar_hash,
        "fabricKgSemanticModelManifestHash": str(
            grounding.sidecar.get("semantic_model_manifest_hash") or ""
        ),
        "fabricKgPersistedProjectionReceiptHash": str(
            grounding.sidecar.get("persisted_projection_receipt_hash") or ""
        ),
        "fabricKgOntologyItemId": str(
            grounding.sidecar.get("ontology_item_id") or ""
        ),
        "fabricKgGraphModelId": str(
            grounding.sidecar.get("graph_model_id") or ""
        ),
        "fabricKgPropertyChildCoverage": (
            f"{grounding.property_child_coverage:.6f}"
        ),
        "fabricKgExpectedPropertyCount": str(
            grounding.expected_property_child_count
        ),
    }
    return elements, metadata


def build_public_lakehouse_source_projection(
    *,
    grounding: PersistedAgentGrounding,
    plan: MaterializationPlan,
    lakehouse_item_id: str,
    schema_name: str = "dbo",
) -> tuple[tuple[DataSourceElement, ...], dict[str, str]]:
    """Expose nonempty contract-owned tables as a semantic-source fallback."""
    availability = {
        item.semantic_id: item.observed_rows
        for item in plan.data_availability
    }
    tables: list[dict[str, Any]] = []
    for table in sorted(
        [*plan.entity_tables, *plan.relationship_tables],
        key=lambda item: item.semantic_id,
    ):
        if availability.get(table.semantic_id, 0) <= 0:
            continue
        columns = [
            DataSourceElement(
                id=_stable_element_id(
                    lakehouse_item_id,
                    f"{schema_name}.{table.table_name}.{column.column_name}",
                ),
                display_name=column.column_name,
                type="lakehouse_tables.column",
                is_selected=True,
                data_type=column.data_type,
                description=(
                    f"Column for {column.semantic_property_id}."
                    if column.semantic_property_id
                    else f"Contract-owned {table.semantic_id} column."
                ),
            ).to_dict()
            for column in table.columns
        ]
        tables.append(
            DataSourceElement(
                id=_stable_element_id(
                    lakehouse_item_id,
                    f"{schema_name}.{table.table_name}",
                ),
                display_name=table.table_name,
                type="lakehouse_tables.table",
                is_selected=True,
                description=(
                    f"Contract-owned materialized table for "
                    f"{table.semantic_id}."
                ),
                children=columns,
            ).to_dict()
        )
    if not tables:
        raise AgentPublicationError(
            "AGENT_EMPTY_SELECTION",
            "Required Lakehouse fallback has no nonempty semantic tables.",
        )

    schema = DataSourceElement(
        id=_stable_element_id(
            lakehouse_item_id,
            f"schema:{schema_name}",
        ),
        display_name=schema_name,
        type="lakehouse_tables.schema",
        is_selected=True,
        description="Schema containing contract-owned semantic tables.",
        children=tables,
    )
    root = DataSourceElement(
        id=_stable_element_id(lakehouse_item_id, "lakehouse_tables"),
        display_name="Semantic tables",
        type="lakehouse_tables",
        is_selected=True,
        description=(
            "Read-only fallback for exact persisted entity, relationship, "
            "and evidence joins when Ontology execution returns no rows."
        ),
        children=[schema.to_dict()],
    )
    _ontology_elements, metadata = (
        build_public_ontology_source_projection(grounding)
    )
    return (root,), {
        **metadata,
        "fabricKgLakehouseItemId": lakehouse_item_id,
        "fabricKgSemanticTableCount": str(len(tables)),
    }


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _stable_element_id(graph_model_id: str, semantic_id: str) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"fabric-kg:data-agent:{graph_model_id}:{semantic_id}",
    ))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_persisted_agent_grounding(
    *,
    manifest: SemanticModelManifest,
    crosswalk: SemanticCrosswalk,
    semantic_context: Mapping[str, Any],
    projection_receipt: PersistedProjectionReceipt,
    projection_receipt_hash: str,
    workspace_id: str,
    graph_model_id: str,
) -> PersistedAgentGrounding:
    """Compile complete source elements only after H3 persisted read-back."""
    if projection_receipt.semantic_model_manifest_hash != manifest.manifest_hash:
        raise AgentPublicationError(
            "AGENT_STALE_PROJECTION",
            "Persisted projection manifest hash differs from the semantic manifest.",
        )
    if projection_receipt.graph_model_id != graph_model_id:
        raise AgentPublicationError(
            "AGENT_STALE_PHYSICAL_TARGET",
            "Persisted Graph Model ID differs from the configured source target.",
        )
    if semantic_context.get("semantic_model_manifest_hash") != manifest.manifest_hash:
        raise AgentPublicationError(
            "AGENT_STALE_SCHEMA",
            "Agent semantic context was not compiled from the persisted manifest.",
        )
    if not workspace_id:
        raise AgentPublicationError(
            "AGENT_TARGET_MISMATCH",
            "Fabric workspace ID is required for Data Agent grounding.",
        )

    entity_crosswalk = {
        entry.semantic_id: entry for entry in crosswalk.entity_type_entries
    }
    relationship_crosswalk = {
        entry.semantic_id: entry
        for entry in crosswalk.relationship_type_entries
    }
    properties_by_owner: dict[str, list[Any]] = {}
    for prop in manifest.property_definitions:
        if prop.agent_visible:
            properties_by_owner.setdefault(prop.owner_type_id, []).append(prop)

    elements: list[DataSourceElement] = []
    sidecar_entities: list[dict[str, Any]] = []
    expected_property_children = 0
    for entity in manifest.entity_types:
        mapping = entity_crosswalk.get(entity.semantic_id)
        if mapping is None or not mapping.graph_label:
            raise AgentPublicationError(
                "AGENT_STALE_PHYSICAL_IDENTIFIER",
                f"Entity '{entity.semantic_id}' has no persisted Graph label.",
            )
        properties = sorted(
            properties_by_owner.get(entity.semantic_id, []),
            key=lambda prop: prop.property_id,
        )
        children = [
            {
                "id": _stable_element_id(
                    graph_model_id,
                    prop.property_id,
                ),
                "display_name": prop.name,
                "type": ELEMENT_TYPE_PROPERTY,
                "is_selected": True,
                "data_type": prop.value_type,
                "description": (
                    f"{prop.business_description} "
                    f"({prop.property_id}; owner={prop.owner_type_id})."
                ),
                "index_state": "indexed",
            }
            for prop in properties
        ]
        expected_property_children += len(children)
        elements.append(DataSourceElement(
            id=_stable_element_id(graph_model_id, entity.semantic_id),
            display_name=mapping.graph_label,
            type=ELEMENT_TYPE_NODE,
            is_selected=True,
            description=(
                f"{entity.business_name}: {entity.description} "
                f"({entity.semantic_id})."
            ),
            children=children,
            index_state="indexed",
        ))
        sidecar_entities.append({
            "semantic_id": entity.semantic_id,
            "category": "entity",
            "business_name": entity.business_name,
            "aliases": entity.aliases,
            "description": entity.description,
            "graph_label": mapping.graph_label,
            "data_agent_element_id": mapping.data_agent_element_id,
            "readiness_state": "ready",
            "properties": [
                {
                    "semantic_id": prop.property_id,
                    "business_name": prop.name,
                    "description": prop.business_description,
                    "value_type": prop.value_type,
                    "required": prop.required,
                    "evidence_policy": prop.evidence_policy,
                    "graph_property": prop.graph_projection.property_key,
                    "data_agent_element_id": (
                        prop.agent_projection.child_id
                    ),
                    "readiness_state": "ready",
                }
                for prop in properties
            ],
        })

    sidecar_relationships: list[dict[str, Any]] = []
    for relationship in manifest.relationship_types:
        mapping = relationship_crosswalk.get(relationship.semantic_id)
        if mapping is None or not mapping.graph_label:
            raise AgentPublicationError(
                "AGENT_STALE_PHYSICAL_IDENTIFIER",
                f"Relationship '{relationship.semantic_id}' has no persisted Graph label.",
            )
        elements.append(DataSourceElement(
            id=_stable_element_id(
                graph_model_id,
                relationship.semantic_id,
            ),
            display_name=mapping.graph_label,
            type=ELEMENT_TYPE_EDGE,
            is_selected=True,
            description=(
                f"{relationship.business_name}: "
                f"{relationship.source_type_id} "
                f"-[{mapping.graph_label}]-> "
                f"{relationship.target_type_id}; "
                f"direction={relationship.direction}; "
                f"cardinality={relationship.cardinality.model_dump(mode='json')}; "
                f"optional={relationship.optional}; "
                f"evidence={relationship.evidence_policy} "
                f"({relationship.semantic_id})."
            ),
            index_state="indexed",
        ))
        sidecar_relationships.append({
            "semantic_id": relationship.semantic_id,
            "category": "relationship",
            "business_name": relationship.business_name,
            "description": relationship.description,
            "source_type_id": relationship.source_type_id,
            "target_type_id": relationship.target_type_id,
            "direction": relationship.direction,
            "cardinality": relationship.cardinality.model_dump(mode="json"),
            "optional": relationship.optional,
            "evidence_policy": relationship.evidence_policy,
            "graph_label": mapping.graph_label,
            "data_agent_element_id": mapping.data_agent_element_id,
            "readiness_state": "ready",
        })

    actual_property_children = sum(
        len(element.children or [])
        for element in elements
        if element.type == ELEMENT_TYPE_NODE
    )
    property_child_coverage = (
        actual_property_children / expected_property_children
        if expected_property_children
        else 1.0
    )
    if property_child_coverage != 1.0:
        raise AgentPublicationError(
            "AGENT_PROPERTY_CHILD_COVERAGE",
            "Agent-visible property-child coverage must equal 1.0.",
        )
    if not elements:
        raise AgentPublicationError(
            "AGENT_EMPTY_SELECTION",
            "Required Graph source has no selected semantic elements.",
        )

    sidecar = {
        "schema_version": "1.1",
        "semantic_model_manifest_hash": manifest.manifest_hash,
        "semantic_crosswalk_hash": str(
            semantic_context.get("semantic_crosswalk_hash") or ""
        ),
        "persisted_projection_receipt_hash": projection_receipt_hash,
        "ontology_persisted_projection_hash": (
            projection_receipt.ontology_persisted_projection_hash
        ),
        "ontology_item_id": projection_receipt.ontology_item_id,
        "graph_persisted_projection_hash": (
            projection_receipt.graph_persisted_projection_hash
        ),
        "graph_model_id": graph_model_id,
        "property_child_coverage": property_child_coverage,
        "entity_types": sidecar_entities,
        "relationship_types": sidecar_relationships,
    }
    selected_element_hash = _canonical_hash({
        "elements": [element.to_dict() for element in elements]
    })
    return PersistedAgentGrounding(
        elements=tuple(elements),
        sidecar=sidecar,
        sidecar_hash=_canonical_hash(sidecar),
        selected_element_hash=selected_element_hash,
        property_child_coverage=property_child_coverage,
        expected_property_child_count=expected_property_children,
    )


def build_agent_publication_receipt(
    *,
    target_mode: Literal["update", "create", "replace"],
    configured_target_item_id: str | None,
    workspace_name: str,
    workspace_id: str,
    data_agent_name: str,
    data_agent_item_id: str,
    package_instruction_hash: str,
    expected: DataAgentStageSnapshot,
    draft: DataAgentStageSnapshot,
    published: DataAgentStageSnapshot,
    grounding: PersistedAgentGrounding,
    projection_receipt: PersistedProjectionReceipt,
    projection_receipt_hash: str,
    publication_status: str,
    required_source_type: Literal["graph", "ontology"] = "graph",
    # Optional grounding text char counts (#12) — zero/empty when not provided.
    global_instruction_chars: int = 0,
    instruction_chars: "dict[str, int] | None" = None,
    description_chars: "dict[str, int] | None" = None,
    competency_examples: "list[CompetencyExampleReceipt] | None" = None,
) -> AgentPublicationReceipt:
    """Fail closed on stale, empty, draft-only, or mismatched publication."""
    if target_mode == "update" and configured_target_item_id != data_agent_item_id:
        raise AgentPublicationError(
            "AGENT_TARGET_MISMATCH",
            "Updated Data Agent ID differs from the configured target.",
        )
    if target_mode == "replace" and configured_target_item_id == data_agent_item_id:
        raise AgentPublicationError(
            "AGENT_TARGET_MISMATCH",
            "Approved replacement did not create a new Data Agent item.",
        )
    if publication_status != "published":
        raise AgentPublicationError(
            "AGENT_DRAFT_ONLY",
            "Data Agent publication did not reach published status.",
        )
    if expected.selected_element_count <= 0:
        raise AgentPublicationError(
            "AGENT_EMPTY_SELECTION",
            "Compiled required source contains no selected elements.",
        )
    if draft.instruction_hash != expected.instruction_hash:
        raise AgentPublicationError(
            "AGENT_DRAFT_INSTRUCTION_DRIFT",
            "Persisted draft instruction differs from the compiled instruction.",
        )
    if published.instruction_hash != expected.instruction_hash:
        raise AgentPublicationError(
            "AGENT_PUBLISHED_INSTRUCTION_DRIFT",
            "Published instruction differs from the compiled instruction.",
        )
    if package_instruction_hash != expected.instruction_hash:
        raise AgentPublicationError(
            "AGENT_PACKAGE_INSTRUCTION_DRIFT",
            "Post-read-back instruction differs from the packaged instruction.",
        )
    if expected.agent_schema_sidecar_hash != grounding.sidecar_hash:
        raise AgentPublicationError(
            "AGENT_SCHEMA_SIDECAR_DRIFT",
            "Compiled public definition does not reference the sealed semantic "
            "schema.",
        )
    if draft.agent_schema_sidecar_hash != grounding.sidecar_hash:
        raise AgentPublicationError(
            "AGENT_SCHEMA_SIDECAR_DRIFT",
            "Persisted draft semantic metadata reference differs from the "
            "compiled schema.",
        )
    if published.agent_schema_sidecar_hash != grounding.sidecar_hash:
        raise AgentPublicationError(
            "AGENT_SCHEMA_SIDECAR_DRIFT",
            "Published semantic metadata reference differs from the compiled "
            "schema.",
        )
    if draft.source_selection_hash != expected.source_selection_hash:
        raise AgentPublicationError(
            "AGENT_DRAFT_SOURCE_SELECTION_DRIFT",
            "Persisted draft source selection differs from the compiled selection.",
        )
    # Three-way property assurance (#14): compare compiled/draft/published against
    # the *original semantic requirement* (grounding.expected_property_child_count),
    # not against the already-compiled snapshot which was previously stripped.
    required_prop_count = grounding.expected_property_child_count
    compiled_prop_count = expected.property_child_count
    draft_prop_count = draft.property_child_count
    published_prop_count = published.property_child_count
    # Check compiled before draft/published so omissions are caught earliest.
    if compiled_prop_count != required_prop_count:
        raise AgentPublicationError(
            "DATA_AGENT_PROPERTY_OMITTED",
            f"Compiled source property count ({compiled_prop_count}) differs "
            f"from the semantic contract requirement ({required_prop_count}). "
            "Ensure all agent-visible properties are selected before deploying.",
        )
    if published_prop_count != required_prop_count:
        raise AgentPublicationError(
            "DATA_AGENT_PROPERTY_OMITTED",
            f"Published source property count ({published_prop_count}) differs "
            f"from the semantic contract requirement ({required_prop_count}). "
            "Ensure all agent-visible properties are preserved through the "
            "Fabric publish pipeline.",
        )
    if draft_prop_count != required_prop_count:
        raise AgentPublicationError(
            "DATA_AGENT_PROPERTY_OMITTED",
            f"Draft source property count ({draft_prop_count}) differs "
            f"from the semantic contract requirement ({required_prop_count}). "
            "Fabric may have stripped property children from the draft definition.",
        )
    if published.source_selection_hash != expected.source_selection_hash:
        raise AgentPublicationError(
            "AGENT_PUBLISHED_SOURCE_SELECTION_DRIFT",
            "Published source selection differs from the compiled selection.",
        )
    if published.selected_element_hash != expected.selected_element_hash:
        raise AgentPublicationError(
            "AGENT_SELECTED_ELEMENT_DRIFT",
            "Published selected elements differ from the compiled elements.",
        )
    sidecar = published.agent_schema_sidecar
    reference = published.agent_schema_reference
    if sidecar is None and reference is None:
        raise AgentPublicationError(
            "AGENT_SCHEMA_SIDECAR_MISSING",
            "Published source has no versioned semantic metadata reference.",
        )
    semantic_model_manifest_hash = (
        sidecar.get("semantic_model_manifest_hash")
        if sidecar is not None
        else reference.get("fabricKgSemanticModelManifestHash")
    )
    if semantic_model_manifest_hash != projection_receipt.semantic_model_manifest_hash:
        raise AgentPublicationError(
            "AGENT_STALE_PROJECTION",
            "Published source references a stale semantic manifest.",
        )
    if required_source_type == "graph":
        physical_item_id = (
            sidecar.get("graph_model_id")
            if sidecar is not None
            else reference.get("fabricKgGraphModelId")
        )
        expected_physical_item_id = projection_receipt.graph_model_id
        physical_label = "Graph Model"
    else:
        physical_item_id = (
            sidecar.get("ontology_item_id")
            if sidecar is not None
            else reference.get("fabricKgOntologyItemId")
        )
        expected_physical_item_id = projection_receipt.ontology_item_id
        physical_label = "Ontology"
    if physical_item_id != expected_physical_item_id:
        raise AgentPublicationError(
            "AGENT_STALE_PHYSICAL_IDENTIFIER",
            f"Published source references a stale {physical_label} ID.",
        )
    published_projection_hash = (
        sidecar.get("persisted_projection_receipt_hash")
        if sidecar is not None
        else reference.get("fabricKgPersistedProjectionReceiptHash")
    )
    if published_projection_hash != projection_receipt_hash:
        raise AgentPublicationError(
            "AGENT_STALE_PROJECTION",
            "Published source references a stale persisted projection receipt.",
        )
    source_receipts = published.source_receipts()
    if any(
        source["workspace_id"] != workspace_id
        or source["artifact_id"] != expected_physical_item_id
        for source in source_receipts
        if source["source_type"] == required_source_type
    ):
        raise AgentPublicationError(
            "AGENT_SOURCE_TARGET_MISMATCH",
            f"Published {physical_label} source identity differs from the "
            "persisted projection.",
        )
    if not any(
        source["source_type"] == required_source_type
        for source in source_receipts
    ):
        raise AgentPublicationError(
            "AGENT_REQUIRED_SOURCE_MISSING",
            f"Published Data Agent has no required {physical_label} source.",
        )

    # Compute content-based property selection hashes for the receipt (#14).
    # Using sorted property IDs rather than count-only so equal-size different
    # selections produce different hashes.
    compiled_prop_sel_hash = _canonical_hash({"property_ids": expected.selected_property_ids})
    published_prop_sel_hash = _canonical_hash({"property_ids": published.selected_property_ids})

    # Compute property_child_coverage from published vs required (#14).
    receipt_property_coverage = (
        published_prop_count / required_prop_count
        if required_prop_count > 0
        else 1.0
    )

    return AgentPublicationReceipt(
        semantic_model_manifest_hash=(
            projection_receipt.semantic_model_manifest_hash
        ),
        persisted_projection_receipt_hash=projection_receipt_hash,
        ontology_persisted_projection_hash=(
            projection_receipt.ontology_persisted_projection_hash
        ),
        graph_persisted_projection_hash=(
            projection_receipt.graph_persisted_projection_hash
        ),
        workspace_name=workspace_name,
        workspace_id=workspace_id,
        data_agent_name=data_agent_name,
        data_agent_item_id=data_agent_item_id,
        configured_target_item_id=configured_target_item_id,
        target_mode=target_mode,
        actions=[target_mode, "publish"],
        selected_sources=[
            AgentSelectedSource.model_validate(source)
            for source in source_receipts
        ],
        package_instruction_hash=package_instruction_hash,
        compiled_instruction_hash=expected.instruction_hash,
        draft_instruction_hash=draft.instruction_hash,
        published_instruction_hash=published.instruction_hash,
        compiled_source_selection_hash=expected.source_selection_hash,
        draft_source_selection_hash=draft.source_selection_hash,
        published_source_selection_hash=published.source_selection_hash,
        compiled_selected_element_hash=expected.selected_element_hash,
        published_selected_element_hash=published.selected_element_hash,
        agent_schema_sidecar_hash=grounding.sidecar_hash,
        property_child_coverage=receipt_property_coverage,
        publication_status="published",
        validated_at_utc=_utc_now(),
        # Property assurance fields (#14)
        required_property_count=required_prop_count,
        compiled_property_count=compiled_prop_count,
        draft_property_count=draft_prop_count,
        published_property_count=published_prop_count,
        compiled_property_selection_hash=compiled_prop_sel_hash,
        published_property_selection_hash=published_prop_sel_hash,
        # Grounding text counts (#12)
        global_instruction_chars=global_instruction_chars,
        instruction_chars=instruction_chars or {},
        description_chars=description_chars or {},
        competency_examples=competency_examples or [],
    )


def deploy_and_validate_data_agent(
    *,
    client: FabricDataAgentClient,
    spec: DataAgentSpec,
    target_mode: Literal["update", "create", "replace"],
    configured_target_item_id: str | None,
    replace_approved: bool,
    workspace_name: str,
    workspace_id: str,
    package_instruction_hash: str,
    grounding: PersistedAgentGrounding,
    projection_receipt: PersistedProjectionReceipt,
    projection_receipt_hash: str,
    published_description: str,
    required_source_type: Literal["graph", "ontology"] = "graph",
    source_policy: Any | None = None,
    # Optional grounding text char counts (#12) — zero/empty when not provided.
    global_instruction_chars: int = 0,
    instruction_chars: "dict[str, int] | None" = None,
    description_chars: "dict[str, int] | None" = None,
    competency_examples: "list[CompetencyExampleReceipt] | None" = None,
) -> tuple[
    DataAgentUpsertResult,
    DataAgentPublishResult,
    AgentPublicationReceipt,
]:
    """Mutate one exact target, publish it, and validate persisted read-back.

    Parameters
    ----------
    source_policy : SourcePolicy | None
        Optional :class:`~fabric_kg_builder.knowledge.validation.SourcePolicy`
        to enforce against the published read-back.  When provided, a
        :class:`~fabric_kg_builder.knowledge.validation.SourcePolicyViolation`
        is raised if the published source types diverge.
    global_instruction_chars : int
        Char count of the global instruction (#12 audit trail).
    instruction_chars : dict[str, int] | None
        Per-source instruction char counts, keyed by source type (#12 audit trail).
    description_chars : dict[str, int] | None
        Per-source description char counts, keyed by source type (#12 audit trail).
    """
    from fabric_kg_builder.knowledge.validation import (  # noqa: PLC0415
        validate_published_source_policy,
    )
    expected = stage_snapshot_from_spec(spec)
    result = client.deploy_target(
        spec,
        target_mode=target_mode,
        configured_item_id=configured_target_item_id,
        replace_approved=replace_approved,
    )
    try:
        publish_result = client.publish(
            result.item_id,
            description=published_description,
        )
        draft, published = client.get_stage_snapshots(result.item_id)
        if source_policy is not None:
            validate_published_source_policy(published, source_policy)
        receipt = build_agent_publication_receipt(
            target_mode=target_mode,
            configured_target_item_id=configured_target_item_id,
            workspace_name=workspace_name,
            workspace_id=workspace_id,
            data_agent_name=spec.display_name,
            data_agent_item_id=result.item_id,
            package_instruction_hash=package_instruction_hash,
            expected=expected,
            draft=draft,
            published=published,
            grounding=grounding,
            projection_receipt=projection_receipt,
            projection_receipt_hash=projection_receipt_hash,
            publication_status=publish_result.status,
            required_source_type=required_source_type,
            global_instruction_chars=global_instruction_chars,
            instruction_chars=instruction_chars,
            description_chars=description_chars,
            competency_examples=competency_examples,
        )
    except (
        AgentPublicationError,
        DataAgentDefinitionError,
        DataAgentLroFailedError,
        DataAgentTargetError,
        HttpError,
        LROTimeoutError,
        SourcePolicyViolation,
    ) as exc:
        if result.created:
            try:
                client.delete_data_agent(result.item_id)
            except (DataAgentLroFailedError, DataAgentTargetError, HttpError, LROTimeoutError) as cleanup_exc:
                raise AgentPublicationError(
                    "AGENT_CLEANUP_FAILED",
                    f"Publication failed with {exc}; cleanup of newly created "
                    f"item {result.item_id} also failed with {cleanup_exc}.",
                ) from exc
        raise
    return result, publish_result, receipt
