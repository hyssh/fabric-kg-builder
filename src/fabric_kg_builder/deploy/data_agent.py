"""Build a source-preserving Fabric Data Agent definition for a semantic graph."""

from __future__ import annotations

from typing import Any

from fabric_kg_builder.knowledge.data_agent import (
    DataAgentSpec,
    DataSourceElement,
    DataSourceSpec,
)
from fabric_kg_builder.ontology.multitype_plan import MultitypePlan

_ENTITY_BUSINESS_ROLES = {
    "Facility": "The managed site or building where work, assets, and projects are associated.",
    "Location": "A physical area within or associated with a facility.",
    "Equipment": "A maintainable physical asset, system, component, or device.",
    "MaintenanceAction": "An observed maintenance, inspection, test, repair, or service action.",
    "MaintenanceEvent": "A dated operational or diagnostic event involving an asset.",
    "Project": "A named construction, replacement, upgrade, or maintenance project.",
    "Organization": "A company, manufacturer, contractor, or other organization.",
    "Person": "An identified individual referenced by source evidence.",
    "PersonRole": "A person or role performing, owning, or supporting work.",
    "EvidenceDocument": "A source document that supports facts through citation_json.",
    "Warranty": "A warranty or coverage record associated with an asset or facility.",
}


def _graph_type(parts: list[dict[str, Any]]) -> dict[str, Any]:
    for part in parts:
        if part.get("path") == "graphType.json":
            return dict(part["payload_json"])
    raise ValueError("Graph Model parts do not contain graphType.json.")


def _node_elements(graph_type: dict[str, Any]) -> list[DataSourceElement]:
    return [
        DataSourceElement(
            id=str(node["alias"]),
            display_name=str(node["labels"][0]),
            type="graph.nodeType",
            is_selected=True,
            description=(
                f"{_ENTITY_BUSINESS_ROLES.get(str(node['labels'][0]), 'A domain entity with source lineage.')} "
                "Match display_name with case-insensitive CONTAINS; aliases_json "
                "retains source aliases and citation_json preserves evidence lineage."
            ),
        )
        for node in graph_type.get("nodeTypes", [])
    ]


def _edge_elements(graph_type: dict[str, Any]) -> list[DataSourceElement]:
    node_labels = {
        str(node["alias"]): str(node["labels"][0])
        for node in graph_type.get("nodeTypes", [])
    }
    elements: list[DataSourceElement] = []
    for edge in graph_type.get("edgeTypes", []):
        source = node_labels.get(str(edge["sourceNodeType"]["alias"]), "Source")
        target = node_labels.get(str(edge["destinationNodeType"]["alias"]), "Target")
        label = str(edge["labels"][0])
        elements.append(
            DataSourceElement(
                id=str(edge["alias"]),
                display_name=label,
                type="graph.edgeType",
                is_selected=True,
                description=(
                    f"Directed relationship: {source} -> {target} ({label}). "
                    "Read it only in that direction. event_date, citation_json, "
                    "and original_relationship_type are edge properties when "
                    "source evidence provides them."
                ),
            )
        )
    return elements


def _edge_triples(graph_type: dict[str, Any]) -> list[tuple[str, str, str]]:
    node_labels = {
        str(node["alias"]): str(node["labels"][0])
        for node in graph_type.get("nodeTypes", [])
    }
    return [
        (
            node_labels.get(str(edge["sourceNodeType"]["alias"]), "Source"),
            str(edge["labels"][0]),
            node_labels.get(str(edge["destinationNodeType"]["alias"]), "Target"),
        )
        for edge in graph_type.get("edgeTypes", [])
    ]


def _edge_label(
    triples: list[tuple[str, str, str]],
    source: str,
    target: str,
    verb: str,
) -> str | None:
    return next(
        (
            label for edge_source, label, edge_target in triples
            if edge_source == source and edge_target == target and verb in label
        ),
        None,
    )


def build_semantic_data_agent_spec(
    *,
    display_name: str,
    workspace_id: str,
    ontology_id: str,
    ontology_name: str,
    graph_model_id: str,
    graph_model_name: str,
    ontology_plan: MultitypePlan,
    graph_parts: list[dict[str, Any]],
) -> DataAgentSpec:
    """Build current graph/ontology sources with grounded schema guidance."""
    graph_type = _graph_type(graph_parts)
    node_elements = _node_elements(graph_type)
    edge_elements = _edge_elements(graph_type)
    edge_triples = _edge_triples(graph_type)
    entity_names = [item.type_name for item in ontology_plan.entity_types]
    graph_labels = [
        element.display_name for element in edge_elements
    ]

    instruction_lines = [
        "Answer relationship, hierarchy, maintenance, and time questions "
        "from the selected Fabric graph and ontology sources.",
        "Use only the selected node and edge labels. Never guess a label.",
        "For entity names and aliases, never use exact equality. Start with "
        "LOWER(display_name) CONTAINS LOWER(<short distinguishing keyword>).",
        "If a user abbreviation returns no rows, enumerate the relevant "
        "node type and retry with a stored display_name or alias.",
        "Dates are evidence-backed edge properties. Apply before/after "
        "filters to edge.event_date only when it is present; do not assume "
        "Project has an event_date.",
        "Use one hop first. If it returns no rows, retry once with a "
        "simpler entity-discovery query before reporting no graph result.",
        "Return citation_json with factual findings so the user can trace "
        "the supporting source. Do not invent dates, relationships, or "
        "maintenance activity.",
    ]
    facility_location = _edge_label(
        edge_triples, "Facility", "Location", "located_at"
    )
    location_equipment = _edge_label(
        edge_triples, "Location", "Equipment", "contains"
    )
    equipment_maintenance = _edge_label(
        edge_triples, "Equipment", "MaintenanceAction", "has_maintenance_action"
    )
    if facility_location and location_equipment and equipment_maintenance:
        instruction_lines.extend(
            [
                "Verified maintenance route: Facility "
                f"-[:{facility_location}]-> Location "
                f"-[:{location_equipment}]-> Equipment "
                f"-[:{equipment_maintenance}]-> MaintenanceAction.",
                "Use this route for maintenance-at-facility questions; return "
                "the Equipment, MaintenanceAction, edge.event_date, and "
                "citation_json. Do not substitute an installation edge.",
            ]
        )
    equipment_evidence = [
        label for source, label, target in edge_triples
        if source == "Equipment" and target == "EvidenceDocument"
        and ("documented_by" in label or "cites" in label)
    ]
    if equipment_evidence:
        instruction_lines.extend(
            [
                "For equipment and installation-evidence questions, discover "
                "the Equipment first, then use OPTIONAL MATCH for evidence "
                f"edges ({', '.join(equipment_evidence)}).",
                "Never require Equipment_installed_at_* to return equipment; "
                "use an installation edge only after the discovery query "
                "shows that exact edge exists for the equipment.",
            ]
        )
    project_edges = [
        label for source, label, target in edge_triples
        if {source, target} == {"Facility", "Project"}
    ]
    if project_edges:
        instruction_lines.append(
            "For project questions, use only these verified Facility/Project "
            f"edges: {', '.join(project_edges)}. If no such traversal returns "
            "a project, state that no verified project relationship was found."
        )
    performer_edges = [
        label for source, label, target in edge_triples
        if target in {"Organization", "Person", "PersonRole"}
        and source in {"Equipment", "MaintenanceAction", "Facility"}
    ]
    if performer_edges:
        instruction_lines.append(
            "For who-performed-work questions, discover MaintenanceAction or "
            "Equipment first and use only these directed performer/service "
            f"edges when present: {', '.join(performer_edges)}."
        )
    instruction_lines.extend(
        [
            f"Selected entity labels: {', '.join(entity_names)}.",
            f"Selected graph edge labels: {', '.join(graph_labels)}.",
        ]
    )
    if "Equipment" in entity_names and "Project" in entity_names:
        instruction_lines.extend(
            [
                "For equipment questions, begin at Equipment. Do not search "
                "Project for an equipment name such as a chiller.",
                "For a date-constrained equipment question, filter an "
                "Equipment relationship edge before attempting a Project path.",
            ]
        )
    instruction = "\n".join(instruction_lines)

    # Ontology source: business meaning, approved concepts, entity interpretation.
    # Deliberately distinct from global and graph instructions (issue #10).
    ontology_source_lines = [
        f"Use this Ontology source to interpret approved entity types: "
        f"{', '.join(entity_names)}.",
        "Resolve user language to selected entity concepts and semantic properties.",
        "Do not treat semantic concept match as proof that two records are related.",
        "Use the Graph source to prove relationships and paths.",
        "Preserve partial dates exactly as stored; keep distinct serial-numbered "
        "assets separate.",
    ]
    ontology_source_instruction = "\n".join(ontology_source_lines)

    # Graph source: exact traversal, edge direction, query, and evidence rules.
    # Deliberately distinct from global and ontology instructions (issue #10).
    graph_source_lines = [
        "Use only selected node and edge labels. Backtick-quote all identifiers.",
        "Preserve every directed edge exactly; do not reverse traversal direction.",
        "Prefer one-hop MATCH patterns; use OPTIONAL MATCH only for optional later hops.",
        "Keep each query within 4 hops and LIMIT 100.",
        "Return endpoint entity IDs and citation_json for relationship findings.",
        "A valid empty result means no verified relationship was found.",
        f"Selected graph edge labels: {', '.join(graph_labels)}.",
    ]
    graph_source_instruction = "\n".join(graph_source_lines)

    ontology_elements = [
        DataSourceElement(
            id=item.type_name,
            display_name=item.type_name,
            type="ontology.entity",
            is_selected=True,
            description=(
                f"{_ENTITY_BUSINESS_ROLES.get(item.type_name, 'A domain entity with source lineage.')} "
                f"Contains {item.count} semantic entities. display_name and "
                "aliases_json identify source terminology; citation_json "
                "provides evidence lineage."
            ),
        )
        for item in ontology_plan.entity_types
    ]
    return DataAgentSpec(
        display_name=display_name,
        instruction=instruction,
        sources=[
            DataSourceSpec(
                source_type="ontology",
                name=ontology_name,
                artifact_id=ontology_id,
                workspace_id=workspace_id,
                display_name=ontology_name,
                instructions=ontology_source_instruction,
                description=(
                    "Semantic Ontology for evidence-backed building maintenance, "
                    "equipment, facility, project, and service relationships."
                ),
                elements=ontology_elements,
                preview=True,
            ),
            DataSourceSpec(
                source_type="graph",
                name=graph_model_name,
                artifact_id=graph_model_id,
                workspace_id=workspace_id,
                display_name=graph_model_name,
                instructions=graph_source_instruction,
                description=(
                    "Typed Graph Model over semantic serving tables. Use it for "
                    "relationship traversal and edge-level temporal evidence."
                ),
                elements=[*node_elements, *edge_elements],
            ),
        ],
    )
