"""agent_instructions.py — generate Fabric Data Agent grounding from the graph.

A Fabric Data Agent over the deployed ontology generates GQL from natural
language (NL2Ontology).  Without grounding it commonly returns 0 rows: it uses
exact-match on names, guesses the wrong entity type, or builds over-long joins.

This module produces a ready-to-paste **Data Agent instruction** document from
the *actual* deployed graph — the entity types and typed relationships in the
multitype plan, plus the user's competency questions from the domain brief.  It
is emitted as a pipeline output (deploy-ontology --create-data-agent-instruction,
default on), so the grounding always matches what was deployed.

All examples, labels, edge aliases, routing keywords, and few-shots are derived
from the compiled graph schema and the supplied domain contract/competency
questions.  No domain-specific vocabulary is hard-coded here; the caller
supplies context via ``entity_types``, ``relationship_pairs``,
``competency_questions``, and the optional ``domain_contract`` dict.

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


def _gql_template(src: str, name: str, tgt: str) -> str:
    """Return a generic single-hop GQL template using actual schema names."""
    return (
        f"MATCH (a:`{src}`)-[:`{name}`]->(b:`{tgt}`)\n"
        "WHERE LOWER(a.`display_name`) CONTAINS LOWER(\"<keyword>\")\n"
        "RETURN a.`display_name`, b.`display_name`"
    )


def build_agent_instructions(
    entity_types: list[Any],
    relationship_pairs: list[Any],
    *,
    ontology_name: str = "kg_ontology",
    industry: str = "",
    business_domain: str = "",
    competency_questions: list[str] | None = None,
    domain_contract: dict[str, Any] | None = None,
) -> str:
    """Return a Markdown Data Agent grounding document for the deployed graph.

    All examples and keywords are derived from the passed ``entity_types``,
    ``relationship_pairs``, ``competency_questions``, and ``domain_contract``.
    No domain-specific vocabulary is injected by this function itself.

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
    domain_contract:
        Optional domain contract dict (``domain``, ``business_context``,
        ``entity_concepts``, ``competency_questions``, routing hints).  When
        provided, domain name and context are used in preference to
        ``industry``/``business_domain``.
    """
    competency_questions = list(competency_questions or [])

    # Pull domain context from domain_contract when available
    dc = domain_contract or {}
    effective_industry = dc.get("domain") or industry
    effective_domain = dc.get("business_context") or business_domain
    if not competency_questions:
        for cq in (dc.get("competency_questions") or []):
            if isinstance(cq, dict):
                q = cq.get("question", "")
            else:
                q = str(cq)
            if q:
                competency_questions.append(q)

    type_names = [
        (getattr(et, "type_name", None) if not isinstance(et, dict) else et.get("type_name"))
        for et in entity_types
    ]
    type_names = [t for t in type_names if t]

    # Build relationship index for template generation
    rel_index: list[tuple[str, str, str]] = []  # (src, name, tgt)
    rel_names: set[str] = set()
    for rp in relationship_pairs:
        name = getattr(rp, "name", None) if not isinstance(rp, dict) else rp.get("name")
        src = getattr(rp, "source_type", None) if not isinstance(rp, dict) else rp.get("source_type")
        tgt = getattr(rp, "target_type", None) if not isinstance(rp, dict) else rp.get("target_type")
        if name and src and tgt:
            rel_index.append((src, name, tgt))
            rel_names.add(name)

    ctx_bits = []
    if effective_industry:
        ctx_bits.append(f"industry **{effective_industry}**")
    if effective_domain:
        ctx_bits.append(f"business domain **{effective_domain}**")
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
        "- Match a short distinguishing keyword from the user's phrase, not the full string."
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

    # Emit step-navigation guidance only when has_step is actually in the schema
    if "has_step" in rel_names:
        lines.append("")
        lines.append("STEP NAVIGATION")
        lines.append(
            "- When navigating from a parent entity to its steps, use `has_step` with "
            "OPTIONAL MATCH so missing steps do not zero the result."
        )
        lines.append(
            "- If the exact name has no steps, broaden: match the parent "
            "display_name CONTAINS a key noun and return all has_step results."
        )
        lines.append(
            "- Step nodes hold short labels only. Use the AI Search data source "
            "(document chunks) for full instruction text — do not expect long "
            "instructions from step display_name."
        )

    lines.append("")
    lines.append("FALLBACK")
    lines.append(
        "- If the graph returns nothing, say so plainly and offer the AI Search results "
        "instead. Do NOT invent entities, IDs, or paths that are not in the result set."
    )
    lines.append("```")
    lines.append("")
    lines.append(
        "> **Connect a second data source.** For verbatim-text questions, add your "
        "AI Search index to this Data Agent alongside the ontology. "
        "The graph answers *structure* (which entities relate, how many hops); "
        "AI Search answers *content* (the actual document text)."
    )
    lines.append("")

    # 2. Discover real names — use actual first entity type from schema
    lines.append("## 2. FIRST — discover the real entity names")
    lines.append("")
    lines.append(
        "User phrases may not match stored `display_name` values exactly. "
        "Before answering entity-specific questions, enumerate the actual names so "
        "you can pick the right CONTAINS keyword:"
    )
    lines.append("")
    if type_names:
        primary_type = type_names[0]
        lines.append("```gql")
        lines.append(f"MATCH (n:`{primary_type}`) RETURN n.`display_name` LIMIT 100")
        lines.append("```")
        lines.append("")
        lines.append(
            f"Repeat for other entity types ({', '.join(type_names[1:3])}{'...' if len(type_names) > 3 else ''}) "
            "as needed. Map the user's term to the closest stored name and query "
            "with a short, distinguishing CONTAINS keyword."
            if len(type_names) > 1
            else
            "Map the user's term to the closest stored name and query "
            "with a short, distinguishing CONTAINS keyword."
        )
    lines.append("")

    # 3. Entity descriptions
    lines.append("## 3. Entity type descriptions (Data Agent → each entity → description)")
    lines.append("")
    lines.extend(_entity_lines(entity_types))
    lines.append("")

    # 4. Relationship map (with copy-paste GQL templates derived from actual schema)
    lines.append("## 4. Relationship map — use these EXACT edge names in GQL")
    lines.append("")
    lines.append(
        "These are the actual edge names in the deployed graph. Do not guess or "
        "abbreviate them:"
    )
    lines.append("")
    rel_lines = _rel_lines(relationship_pairs)
    lines.extend(rel_lines or ["- (no typed relationships in this graph)"])
    lines.append("")

    # Emit one generic single-hop template per edge from the actual schema
    if rel_index:
        lines.append("Single-hop templates (replace `<keyword>` with the user's search term):")
        lines.append("")
        for src, name, tgt in rel_index:
            lines.append(f"**{src} → [{name}] → {tgt}**")
            lines.append("```gql")
            lines.append(_gql_template(src, name, tgt))
            lines.append("```")
            lines.append("")

    # 5. Example queries from competency questions
    lines.append("## 5. Example queries (Data Agent → \"Example queries\")")
    lines.append("")
    if competency_questions:
        lines.append(
            "Use the domain's competency questions as few-shots. Map each to a SINGLE-HOP "
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
