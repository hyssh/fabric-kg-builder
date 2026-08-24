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


def _bounded_query_policy(semantic_context: dict[str, Any]) -> tuple[int, list[str]]:
    if semantic_context.get("schema_mode") != "schema2_bounded":
        return 4, []
    max_hops = int(semantic_context.get("approved_max_hops") or 0)
    authority_hash = str(
        semantic_context.get("query_authority_hash") or ""
    ).strip()
    if not 1 <= max_hops <= 4 or not authority_hash:
        raise ValueError(
            "Schema-2 agent context requires approved_max_hops in 1..4 and "
            "query_authority_hash."
        )
    paths = semantic_context.get("approved_query_paths")
    approved_ids = [
        str(path.get("question_id"))
        for path in paths
        if isinstance(path, dict) and path.get("covered") is True
    ] if isinstance(paths, list) else []
    return max_hops, approved_ids


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
    max_hops, approved_plan_ids = _bounded_query_policy(semantic_context)
    schema2_bounded = semantic_context.get("schema_mode") == "schema2_bounded"
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
            "- Fabric Ontology and Fabric Graph are required for reliable structured "
            "query execution.",
            "- Use Ontology first to interpret approved common/domain entity nouns, "
            "properties, and relationship verb meaning.",
            "- Use Graph for exact relationship traversal, paths, dependencies, "
            "ownership, location, coverage, installation, and replacement lineage.",
            "- Use AI Search after Ontology/Graph resolution for detailed definitions, "
            "descriptions, and verbatim source quotations. Filter Search with returned "
            "canonical IDs when available.",
            "- Never use Search text similarity alone to assert a structured relationship.",
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
            (
                f"- Keep queries bounded to the approved K={max_hops} and 100 rows. "
                if schema2_bounded
                else "- Keep queries bounded to 4 hops and 100 rows. "
            )
            + "Do not invent labels, "
            "properties, dates, identities, or replacement links.",
        ]
    )
    if schema2_bounded:
        lines.append(
            "- Use only approved bounded query plans. Abstain when no approved "
            "plan applies; decompose only into approved bounded subquestions and "
            "never increase K."
        )
    if approved_plan_ids:
        lines.append(
            "- Approved bounded plan IDs: "
            + ", ".join(f"`{item}`" for item in approved_plan_ids[:10])
            + "."
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
    *,
    availability: "dict[str, Any] | None" = None,
) -> str:
    """Describe the Ontology source in domain and routing terms.

    Parameters
    ----------
    availability:
        Optional mapping of semantic_id →
        :class:`~fabric_kg_builder.semantic.schemas.DataAvailability`.
        Currently unused for Ontology descriptions (all entities are schema-level),
        but accepted for API symmetry with ``build_graph_source_description``.
    """
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
    *,
    availability: "list[Any] | dict[str, Any] | None" = None,
) -> str:
    """Render concise Graph-specific traversal and evidence guidance.

    Parameters
    ----------
    availability:
        Optional list of :class:`~fabric_kg_builder.semantic.schemas.DataAvailability`
        objects or a mapping of semantic_id → DataAvailability.
        When provided, the instructions acknowledge which relationship paths
        are currently unobserved so the agent does not over-claim.
    """
    contract_name, contract_hash, _ = _contract_identity(semantic_context)
    max_hops, approved_plan_ids = _bounded_query_policy(semantic_context)
    schema2_bounded = semantic_context.get("schema_mode") == "schema2_bounded"
    lines = [
        f"Query this Graph using `{contract_name}` (`{contract_hash}`).",
        "Use only selected node, edge, and property identifiers. Backtick-quote "
        "identifiers and preserve every directed edge exactly.",
        "Prefer one-hop MATCH patterns; use OPTIONAL MATCH only for later optional "
        + (
            f"hops. Keep each query within the approved K={max_hops} and LIMIT 100."
            if schema2_bounded
            else "hops. Keep each query within 4 hops and LIMIT 100."
        ),
        "Return endpoint entity IDs and `evidence_id` for relationship findings. "
        "Do not infer edges from names, shared documents, or physical proximity.",
        "A valid empty result means no verified relationship was found. Surface "
        "execution failures separately instead of answering from memory.",
    ]
    if schema2_bounded:
        lines.insert(
            -1,
            "Use only approved bounded plan IDs. If none applies, abstain; never "
            "author raw GQL or silently raise K.",
        )
    if approved_plan_ids:
        lines.append(
            "Approved bounded plans: "
            + ", ".join(f"`{item}`" for item in approved_plan_ids[:10])
            + "."
        )
    avail_dict = _normalize_availability(availability)
    if avail_dict:
        unobserved = sorted(
            sem_id
            for sem_id, avail in avail_dict.items()
            if hasattr(avail, "status") and avail.status in {"unavailable", "insufficient", "not_observed"}
        )
        if unobserved:
            lines.append(
                "Note: the following relationship paths have no currently observed "
                "data and should not be claimed in answers: "
                + ", ".join(f"'{r}'" for r in unobserved) + "."
            )
    return "\n".join(lines)


def build_graph_source_description(
    semantic_context: dict[str, Any],
    *,
    availability: "list[Any] | dict[str, Any] | None" = None,
) -> str:
    """Describe the Graph source in domain and routing terms.

    Parameters
    ----------
    availability:
        Optional list of :class:`~fabric_kg_builder.semantic.schemas.DataAvailability`
        objects or a mapping of semantic_id → DataAvailability.
        When provided, only observed-available relationship labels are included
        in the traversal description.  Unavailable-but-schema-supported paths
        are noted explicitly so the agent never over-claims.
    """
    contract_name, _, _ = _contract_identity(semantic_context)
    avail_dict = _normalize_availability(availability)
    if not avail_dict:
        return (
            f"{contract_name}: persisted directed Graph for verified "
            "relationship traversal, paths, dependencies, and evidence."
        )
    # Build description from observed availability.
    # Per ADR D7: only name AVAILABLE relationships; omit unavailable ones
    # so the agent never over-claims a relationship that has no observed data.
    available_rels: list[str] = []
    has_unavailable = False
    for sem_id, avail in sorted(avail_dict.items()):
        if not hasattr(avail, "status"):
            continue
        label = sem_id.split(":")[-1] if ":" in sem_id else sem_id
        if avail.status == "sufficient":
            available_rels.append(label)
        elif avail.status in {"unavailable", "insufficient"}:
            has_unavailable = True
    parts: list[str] = []
    if available_rels:
        parts.append(
            f"Verified traversal for: {', '.join(available_rels)}."
        )
    else:
        parts.append(
            "No relationship paths have observed data; Graph queries will return empty."
        )
    if has_unavailable:
        parts.append(
            "Some relationship paths have no verified published data; "
            "do not claim those paths are available."
        )
    return f"{contract_name}: {' '.join(parts)}"


def _normalize_availability(
    availability: "list[Any] | dict[str, Any] | None",
) -> "dict[str, Any]":
    """Normalize availability to a semantic_id → DataAvailability dict."""
    if availability is None:
        return {}
    if isinstance(availability, dict):
        return availability
    # Assume it's a list of DataAvailability objects with .semantic_id
    return {
        avail.semantic_id: avail
        for avail in availability
        if hasattr(avail, "semantic_id")
    }
