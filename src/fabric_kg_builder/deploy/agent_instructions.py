"""agent_instructions.py — generate Fabric Data Agent grounding from the graph.

A Fabric Data Agent over the deployed ontology generates GQL from natural
language (NL2Ontology).  Without grounding it commonly returns 0 rows: it uses
exact-match on names, guesses the wrong entity type, or builds over-long joins.

This module produces a ready-to-paste **Data Agent instruction** document from
the *actual* deployed graph — the entity types and typed relationships in the
multitype plan, plus the user's competency questions from the domain brief.  It
is emitted as a pipeline output (deploy-ontology --create-data-agent-instruction,
default on), so the grounding always matches what was deployed.

Deterministic, no LLM, no network.
"""

from __future__ import annotations

from typing import Any


def _rel_lines(relationship_pairs: list[Any]) -> list[str]:
    lines: list[str] = []
    for rp in relationship_pairs:
        # Support both dataclass (RelationshipPairPlan) and dict shapes.
        name = getattr(rp, "name", None) if not isinstance(rp, dict) else rp.get("name")
        src = getattr(rp, "source_type", None) if not isinstance(rp, dict) else rp.get("source_type")
        tgt = getattr(rp, "target_type", None) if not isinstance(rp, dict) else rp.get("target_type")
        if name and src and tgt:
            lines.append(f"- `{src}` -[`{name}`]-> `{tgt}`")
    return lines


def _entity_lines(entity_types: list[Any]) -> list[str]:
    lines: list[str] = []
    for et in entity_types:
        name = getattr(et, "type_name", None) if not isinstance(et, dict) else et.get("type_name")
        count = getattr(et, "count", None) if not isinstance(et, dict) else et.get("count")
        if name:
            suffix = f"  (~{count} instances)" if count else ""
            lines.append(f"- **{name}** — entity_id, entity_type, display_name, canonical_key{suffix}")
    return lines


def build_agent_instructions(
    entity_types: list[Any],
    relationship_pairs: list[Any],
    *,
    ontology_name: str = "kg_ontology",
    industry: str = "",
    business_domain: str = "",
    competency_questions: list[str] | None = None,
) -> str:
    """Return a Markdown Data Agent grounding document for the deployed graph.

    Parameters
    ----------
    entity_types:
        EntityTypePlan items (or dicts) with ``type_name`` (and optional ``count``).
    relationship_pairs:
        RelationshipPairPlan items (or dicts) with ``name`` / ``source_type`` /
        ``target_type``.
    ontology_name, industry, business_domain:
        Context echoed into the document header.
    competency_questions:
        Sample questions from the domain brief — rendered as suggested few-shots.
    """
    competency_questions = competency_questions or []
    type_names = [
        (getattr(et, "type_name", None) if not isinstance(et, dict) else et.get("type_name"))
        for et in entity_types
    ]
    type_names = [t for t in type_names if t]

    ctx_bits = []
    if industry:
        ctx_bits.append(f"industry **{industry}**")
    if business_domain:
        ctx_bits.append(f"business domain **{business_domain}**")
    ctx_line = (" for " + ", ".join(ctx_bits)) if ctx_bits else ""

    lines: list[str] = []
    lines.append(f"# Data Agent grounding for `{ontology_name}`")
    lines.append("")
    lines.append(
        f"Auto-generated from the deployed knowledge graph{ctx_line}. Paste these into "
        f"your Fabric **Data Agent** configuration so NL→GQL returns rows instead of "
        f"'no data found'."
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # 1. Agent instructions
    lines.append("## 1. Agent instructions (Data Agent → \"Additional instructions\")")
    lines.append("")
    lines.append("```")
    lines.append(
        f"You answer questions over a knowledge graph{ctx_line.replace('**','')}. "
        "Follow these rules when generating GQL:"
    )
    lines.append("")
    lines.append("NAMES & TYPES")
    lines.append(
        "- Never use exact equality on display_name. Use case-insensitive CONTAINS, e.g."
    )
    lines.append("  WHERE LOWER(n.`display_name`) CONTAINS LOWER(\"<keyword>\").")
    lines.append(
        "- Match a short distinguishing keyword, not the user's whole phrase "
        "(e.g. \"swell\" or \"expansion\", not \"swollen battery\")."
    )
    lines.append(f"- Valid entity types: {', '.join(type_names)}.")
    lines.append("")
    lines.append("QUERY SHAPE (prefer short paths)")
    lines.append("- Prefer SINGLE-HOP queries. Only add a second hop if the first returns rows.")
    lines.append(
        "- Never require a 3+ hop conjunctive (comma-joined) pattern; use OPTIONAL MATCH "
        "for later hops so one missing edge does not zero the result."
    )
    lines.append("- If a query returns 0 rows, retry with a simpler 1-hop query before giving up.")
    lines.append("- Do not retry the same failing pattern repeatedly; after one simpler retry, stop.")
    lines.append("")
    lines.append("PROCEDURES & STEPS")
    lines.append(
        "- A named procedure (e.g. \"Enclosure Replacement\") may be split into sibling "
        "procedures like \"Removal (Enclosure)\" / \"Installation (Enclosure)\". When the exact "
        "name has no steps, broaden: match Procedure display_name CONTAINS the key noun "
        "(e.g. \"enclosure\", \"ssd\", \"kickstand\") and return all has_step results."
    )
    lines.append(
        "- For VERBATIM step-by-step instructions, the graph holds short step labels only. "
        "Use the AI Search data source (document chunks) for the full instruction text — do "
        "NOT expect long instructions from Step.display_name."
    )
    lines.append("")
    lines.append("FALLBACK")
    lines.append(
        "- If the graph returns nothing, say so plainly and offer the AI Search results "
        "instead. Do NOT invent entities, IDs, or steps that are not in the result set."
    )
    lines.append("```")
    lines.append("")
    lines.append(
        "> **Connect a second data source.** For \"detailed steps\" and other verbatim-text "
        "questions, add your AI Search index (e.g. `kg-dev-kg-chunks`) to this Data Agent "
        "alongside the ontology. The graph answers *structure* (which device has which "
        "procedure/part, how many steps); AI Search answers *content* (the actual instructions)."
    )
    lines.append("")

    # 2. Discover real names first
    lines.append("## 2. FIRST — discover the real entity names")
    lines.append("")
    lines.append(
        "User phrases rarely match stored `display_name` exactly (a user says "
        "\"Surface Pro 10\" but the node is \"Surface Pro 10th Edition for Business\"). "
        "Before answering device-specific questions, learn the real names so you can "
        "pick the right CONTAINS keyword:"
    )
    lines.append("")
    lines.append("```gql")
    lines.append("MATCH (d:`DeviceModel`) RETURN d.`display_name` LIMIT 100")
    lines.append("```")
    lines.append("")
    lines.append(
        "Then map the user's term to the closest real name and query with a short, "
        "distinguishing CONTAINS keyword (e.g. \"pro 10\", \"laptop 5\")."
    )
    lines.append("")

    # 3. Entity descriptions
    lines.append("## 3. Entity type descriptions (Data Agent → each entity → description)")
    lines.append("")
    lines.extend(_entity_lines(entity_types))
    lines.append("")

    # 4. Relationship map (with copy-paste GQL templates)
    lines.append("## 4. Relationship map — use these EXACT edge names in GQL")
    lines.append("")
    lines.append(
        "These are the actual edge names in the deployed graph. Do not guess or "
        "abbreviate them (e.g. it is `has_component`, not "
        "`supported_model_has_component`):"
    )
    lines.append("")
    rel_lines = _rel_lines(relationship_pairs)
    lines.extend(rel_lines or ["- (no typed relationships in this graph)"])
    lines.append("")
    # Concrete single-hop templates for the most useful device-rooted edges.
    rel_names = {
        (getattr(rp, "name", None) if not isinstance(rp, dict) else rp.get("name"))
        for rp in relationship_pairs
    }
    templates: list[tuple[str, str]] = []
    if "has_component" in rel_names:
        templates.append((
            "Components of a device model",
            "MATCH (d:`DeviceModel`)-[:`has_component`]->(c:`Component`)\n"
            "WHERE LOWER(d.`display_name`) CONTAINS LOWER(\"pro 10\")\n"
            "RETURN DISTINCT c.`display_name`",
        ))
    if "has_step" in rel_names:
        templates.append((
            "Steps of a procedure (broaden by key noun if the exact name has none)",
            "MATCH (p:`Procedure`)-[:`has_step`]->(s:`Step`)\n"
            "WHERE LOWER(p.`display_name`) CONTAINS LOWER(\"kickstand\")\n"
            "RETURN p.`display_name`, s.`display_name`",
        ))
    if "causes" in rel_names:
        templates.append((
            "Causes of a symptom",
            "MATCH (c:`Cause`)-[:`causes`]->(s:`Symptom`)\n"
            "WHERE LOWER(s.`display_name`) CONTAINS LOWER(\"expansion\")\n"
            "RETURN DISTINCT c.`display_name`",
        ))
    if "remediated_by" in rel_names:
        templates.append((
            "Root-cause analysis for a symptom (cause + fix + steps in one query)",
            "MATCH (s:`Symptom`)\n"
            "WHERE LOWER(s.`display_name`) CONTAINS LOWER(\"expansion\")\n"
            "OPTIONAL MATCH (c:`Cause`)-[:`causes`]->(s)\n"
            "OPTIONAL MATCH (s)-[:`diagnosed_by`]->(dt:`Procedure`)\n"
            "OPTIONAL MATCH (s)-[:`remediated_by`]->(rp:`Procedure`)\n"
            "OPTIONAL MATCH (rp)-[:`has_step`]->(st:`Step`)\n"
            "OPTIONAL MATCH (s)-[:`resolved_by`]->(r:`Resolution`)\n"
            "RETURN s.`display_name`, c.`display_name`, dt.`display_name`,\n"
            "       rp.`display_name`, st.`display_name`, r.`display_name`",
        ))
    if templates:
        lines.append("Ready-to-use single-hop templates:")
        lines.append("")
        for title, gql in templates:
            lines.append(f"**{title}**")
            lines.append("```gql")
            lines.append(gql)
            lines.append("```")
            lines.append("")

    # 5. Example queries from competency questions
    lines.append("## 5. Example queries (Data Agent → \"Example queries\")")
    lines.append("")
    if competency_questions:
        lines.append(
            "Use the user's competency questions as few-shots. Map each to a SINGLE-HOP "
            "GQL query using the relationship map above and CONTAINS on display_name:"
        )
        lines.append("")
        for q in competency_questions:
            lines.append(f"- {q}")
    else:
        lines.append(
            "_No competency questions were captured. Re-run `set-domain` with "
            "`--questions-file` to auto-populate strong few-shots here._"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "> Generated by `fabric-kg deploy-ontology --create-data-agent-instruction`. "
        "Re-deploys refresh this file to match the live graph."
    )
    lines.append("")
    return "\n".join(lines)
