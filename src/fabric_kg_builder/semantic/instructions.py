"""Contract-owned agent grounding shared by Fabric and Foundry agents."""

from __future__ import annotations

from typing import Any, Iterable


def build_contract_agent_instructions(
    semantic_context: dict[str, Any],
    *,
    competency_questions: Iterable[str] = (),
    domain_context: str = "",
) -> str:
    """Render exact Graph labels, direction, and Search handoff rules."""
    contract_hash = str(semantic_context.get("contract_hash", "")).strip()
    contract_name = str(semantic_context.get("contract_name", "")).strip()
    contract_description = str(
        semantic_context.get("contract_description", "")
    ).strip()
    entity_types = list(semantic_context.get("entity_types") or [])
    property_definitions = list(
        semantic_context.get("property_definitions") or []
    )
    relationship_types = list(semantic_context.get("relationship_types") or [])
    if not contract_hash or not contract_name:
        raise ValueError(
            "Agent semantic context requires contract_name and contract_hash."
        )

    lines = [
        f"# Grounding for `{contract_name}`",
        "",
        f"Semantic contract hash: `{contract_hash}`",
        "",
        contract_description,
    ]
    if domain_context.strip():
        lines.extend(["", "Approved domain context:", domain_context.strip()])
    lines.extend(
        [
            "",
            "## Source responsibilities",
            "",
            "- Use the selected **Fabric Ontology semantic source** for hierarchy, "
            "dependency, ownership, path, impact, and other relationship questions.",
            "- Use the selected **Fabric Lakehouse semantic source** as the "
            "required deterministic fallback for the same sealed semantic model.",
            "- If an Ontology relationship query returns no rows, errors, or cannot "
            "access a required type, execute the matching Lakehouse relationship "
            "join below before reporting no verified relationship.",
            "- Interpret Ontology entities and relationships using the exact Graph "
            "labels and direction below; do not invent synonyms that change "
            "direction or cardinality.",
            "- Use **AI Search** for detailed text, attributes, quotations, dates, "
            "and source passages.",
            "- For mixed questions, identify the relevant entities/path in the "
            "semantic source, "
            "then retrieve Search evidence using returned entity, evidence, asset, "
            "or source identifiers.",
            "",
            "## Hard rules",
            "",
            "- Use only the exact Graph labels listed below.",
            "- Backtick-quote every Graph node label, edge label, and property "
            "identifier; Fabric GQL treats identifiers such as `Project` as "
            "reserved when they are bare.",
            "- Preserve every edge direction exactly as listed.",
            "- Prefer one-hop Graph queries; use OPTIONAL MATCH for later hops.",
            "- Relationships marked optional or experimental must not become "
            "mandatory discovery joins unless the question explicitly requires "
            "that predicate.",
            "- Never report an asserted relationship without its required evidence.",
            "- Never interpret an empty Ontology result as proof that a persisted "
            "relationship is absent until its Lakehouse fallback query also returns "
            "no rows.",
            "- For relationship questions, return only rows produced by the exact "
            "directed relationship or its matching Lakehouse relationship table; "
            "entity-name similarity is not relationship evidence.",
            "- Never treat Search similarity as proof that an Ontology relationship "
            "exists.",
            "- Cite the evidence record, immutable asset-version ID, and Blob or "
            "source locator for every factual answer.",
            "- Include the exact stored `entity_id` and `source_file_id` (`src:`) "
            "or `evidence_id` beside each factual finding so Search can resolve "
            "the immutable source citation.",
            "- Format every relationship finding as one explicit row: "
            "`<source name> (entity_id: entity:<id>) -> <relationship> -> "
            "<target name> (entity_id: entity:<id>); evidence_id: evid:<id>`.",
            "- Do not group, truncate, or replace relationship rows with phrases "
            "such as `and more` or `full list available`. Return at most 100 "
            "fully identified rows. If an exact endpoint or evidence identifier "
            "is unavailable, omit that row and state why it is unsupported.",
            "- If Graph and Search disagree or evidence is missing, report the "
            "conflict instead of guessing.",
            "",
            "## Exact node labels",
            "",
        ]
    )
    for entity in entity_types:
        aliases = ", ".join(
            str(alias) for alias in entity.get("aliases", []) if alias
        )
        alias_suffix = f"; aliases={aliases}" if aliases else ""
        lines.append(
            f"- `{entity['graph_label']}` — {entity['business_name']} "
            f"(`{entity['semantic_id']}`); Lakehouse table="
            f"`{entity.get('lakehouse_table', 'not published')}`{alias_suffix}"
        )
    if not entity_types:
        lines.append("- No published node labels.")

    lines.extend(["", "## Approved agent-visible properties", ""])
    visible_properties = [
        prop
        for prop in property_definitions
        if prop.get("agent_visible", True)
    ]
    for prop in visible_properties:
        lines.append(
            f"- `{prop['owner_type_id']}/{prop['semantic_id']}` "
            f"as `{prop['name']}` / Graph property "
            f"`{prop.get('graph_property') or prop['name']}` "
            f"({prop['value_type']}) — "
            f"{prop['business_description']}"
        )
    if not visible_properties:
        lines.append("- No agent-visible properties.")

    lines.extend(["", "## Exact directed edge labels", ""])
    for relationship in relationship_types:
        lines.append(
            f"- `({relationship['source_graph_label']})"
            f"-[:{relationship['graph_label']}]->"
            f"({relationship['target_graph_label']})` — "
            f"{relationship['business_name']} "
            f"(`{relationship['semantic_id']}`); "
            f"Lakehouse table="
            f"`{relationship.get('lakehouse_table', 'not published')}`; "
            f"canonical endpoints="
            f"{relationship.get('source_type_id', '?')} -> "
            f"{relationship.get('target_type_id', '?')}; "
            f"cardinality={relationship.get('cardinality', {})}; "
            f"optional={relationship.get('optional', True)}; "
            f"evidence={relationship['evidence_policy']}; "
            f"publication={relationship.get('publication_status', 'core')}"
        )
    if not relationship_types:
        lines.append("- No published edge labels.")

    entity_tables = {
        str(entity["semantic_id"]): str(entity.get("lakehouse_table") or "")
        for entity in entity_types
    }
    lines.extend(
        ["", "## Mandatory Lakehouse relationship fallback queries", ""]
    )
    fallback_count = 0
    for relationship in relationship_types:
        relationship_table = str(
            relationship.get("lakehouse_table") or ""
        )
        source_table = entity_tables.get(
            str(relationship.get("source_type_id") or ""), ""
        )
        target_table = entity_tables.get(
            str(relationship.get("target_type_id") or ""), ""
        )
        if not relationship_table or not source_table or not target_table:
            continue
        fallback_count += 1
        lines.extend(
            [
                f"- `{relationship['semantic_id']}`:",
                "```sql",
                (
                    "SELECT TOP 100 "
                    "source_entity.entity_id AS source_entity_id, "
                    "source_entity.display_name AS source_name, "
                    "target_entity.entity_id AS target_entity_id, "
                    "target_entity.display_name AS target_name, "
                    "relationship.evidence_id"
                ),
                f"FROM [dbo].[{relationship_table}] AS relationship",
                (
                    f"JOIN [dbo].[{source_table}] AS source_entity "
                    "ON relationship.source_entity_id = source_entity.entity_id"
                ),
                (
                    f"JOIN [dbo].[{target_table}] AS target_entity "
                    "ON relationship.target_entity_id = target_entity.entity_id"
                ),
                "```",
                "",
            ]
        )
    if not fallback_count:
        lines.append("- No published Lakehouse fallback mappings.")

    lines.extend(["", "## Safe GQL templates", ""])
    for relationship in relationship_types:
        source = relationship["source_graph_label"]
        edge = relationship["graph_label"]
        target = relationship["target_graph_label"]
        lines.extend(
            [
                "```gql",
                f"MATCH (source:`{source}`)-[rel:`{edge}`]->(target:`{target}`)",
                "WHERE LOWER(source.`display_name`) CONTAINS LOWER(\"<keyword>\")",
                "RETURN source, rel, target",
                "LIMIT 100",
                "```",
                "",
            ]
        )

    lines.extend(
        [
            "## Query and failure policy",
            "",
            "- Keep each physical query within 4 hops, 6 node references, "
            "5 relationship references, and 100 returned rows.",
            "- Decompose broader questions into at most 4 bounded subqueries "
            "joined by canonical entity IDs.",
            "- A valid no-match result is not an execution failure. State that "
            "no verified result was found and identify the attempted source.",
            "- Do not answer a required relationship question from Search alone "
            "when semantic-source execution failed or returned an invalid plan.",
            "- Unsupported claims must be labeled unsupported; do not fill gaps "
            "from model memory.",
            "- Authentication, timeout, conflict, and platform errors must be "
            "reported as source failures rather than converted to no-data.",
            "",
        ]
    )

    questions = [question.strip() for question in competency_questions if question.strip()]
    lines.extend(["## Competency questions", ""])
    if questions:
        lines.extend(f"- {question}" for question in questions)
    else:
        lines.append("- No competency questions were supplied.")
    lines.append("")
    return "\n".join(lines)
