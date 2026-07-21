"""Contract-owned agent grounding shared by Fabric and Foundry agents."""

from __future__ import annotations

from typing import Any, Iterable


def _contract_identity(semantic_context: dict[str, Any]) -> tuple[str, str, str]:
    contract_hash = str(semantic_context.get("contract_hash", "")).strip()
    contract_name = str(semantic_context.get("contract_name", "")).strip()
    contract_description = str(
        semantic_context.get("contract_description", "")
    ).strip()
    if not contract_hash or not contract_name:
        raise ValueError(
            "Agent semantic context requires contract_name and contract_hash."
        )
    return contract_name, contract_hash, contract_description


def build_contract_agent_instructions(
    semantic_context: dict[str, Any],
    *,
    competency_questions: Iterable[str] = (),
    domain_context: str = "",
) -> str:
    """Render compact global routing, evidence, and answer policy."""
    contract_name, contract_hash, contract_description = _contract_identity(
        semantic_context
    )
    questions = [
        question.strip()
        for question in competency_questions
        if question.strip()
    ][:5]
    lines = [
        f"# `{contract_name}`",
        f"Semantic contract: `{contract_hash}`.",
    ]
    if contract_description:
        lines.append(contract_description)
    if domain_context.strip():
        lines.append(f"Domain context: {domain_context.strip()}")
    lines.extend(
        [
            "",
            "## Routing",
            "- Use Ontology to interpret approved business concepts, properties, "
            "and relationship meaning.",
            "- Use Graph for exact relationship traversal, paths, dependencies, "
            "ownership, location, coverage, installation, and replacement lineage.",
            "- Do not use Lakehouse or infer a relationship from similar names or "
            "document proximity.",
            "",
            "## Evidence and answers",
            "- Preserve Graph edge direction and use only selected source elements.",
            "- Every asserted relationship must come from a returned Graph edge and "
            "include its `evidence_id` when available.",
            "- Include stored entity IDs and source/evidence locators in factual "
            "findings. Distinguish assets by serial number or canonical ID.",
            "- If Ontology meaning and Graph evidence disagree, report the conflict. "
            "If no verified row exists, say the relationship is unsupported.",
            "- Report authentication, timeout, platform, and query errors as source "
            "failures; never convert them into no-data.",
            "- Keep queries bounded to 4 hops and 100 rows. Do not invent labels, "
            "properties, dates, identities, or replacement links.",
        ]
    )
    if questions:
        lines.extend(["", "## Representative questions"])
        lines.extend(f"- {question}" for question in questions)
    lines.append("")
    return "\n".join(lines)


def build_ontology_source_instructions(
    semantic_context: dict[str, Any],
) -> str:
    """Render concise Ontology-specific usage guidance."""
    contract_name, contract_hash, _ = _contract_identity(semantic_context)
    return "\n".join(
        [
            f"Interpret this source using `{contract_name}` (`{contract_hash}`).",
            "Use it for approved entity types, business definitions, properties, "
            "and relationship semantics exposed by the selected elements.",
            "Resolve user language to those selected concepts, but do not treat "
            "semantic compatibility as proof that two records are related.",
            "Use the Graph source to prove relationships and paths. Preserve partial "
            "dates exactly as stored and keep distinct serial-numbered assets separate.",
        ]
    )


def build_ontology_source_description(
    semantic_context: dict[str, Any],
) -> str:
    """Describe the Ontology source in domain and routing terms."""
    contract_name, _, contract_description = _contract_identity(semantic_context)
    scope = (
        contract_description or "approved business concepts and relationships"
    ).rstrip(".")
    return (
        f"{contract_name}: Ontology meaning for {scope}. Use this source to "
        "interpret selected entity types, properties, and relationship semantics."
    )


def build_graph_source_instructions(
    semantic_context: dict[str, Any],
) -> str:
    """Render concise Graph-specific traversal and evidence guidance."""
    contract_name, contract_hash, _ = _contract_identity(semantic_context)
    return "\n".join(
        [
            f"Query this Graph using `{contract_name}` (`{contract_hash}`).",
            "Use only selected node, edge, and property identifiers. Backtick-quote "
            "identifiers and preserve every directed edge exactly.",
            "Prefer one-hop MATCH patterns; use OPTIONAL MATCH only for later optional "
            "hops. Keep each query within 4 hops and LIMIT 100.",
            "Return endpoint entity IDs and `evidence_id` for relationship findings. "
            "Do not infer edges from names, shared documents, or physical proximity.",
            "A valid empty result means no verified relationship was found. Surface "
            "execution failures separately instead of answering from memory.",
        ]
    )


def build_graph_source_description(
    semantic_context: dict[str, Any],
) -> str:
    """Describe the Graph source in domain and routing terms."""
    contract_name, _, _ = _contract_identity(semantic_context)
    return (
        f"{contract_name}: persisted directed Graph for verified location, "
        "warranty, installation, replacement, document, and evidence traversal."
    )
