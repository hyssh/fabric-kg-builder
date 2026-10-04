"""knowledge.routing -- domain-based routing instruction generator.

AGK-007: Classifies a question into one of three routing categories and
generates a ready-to-use Fabric Data Agent instruction block (or Search
knowledge-base routing hint).

Routing categories
------------------
``SEARCH``
    Direct factual or hybrid-search questions answered by document content.
    These ask for specific field values, full-text document passages, verbatim
    instructions, or structured metadata.

``GRAPH``
    Hierarchy, dependency, or topology questions answered by the knowledge
    graph.  These ask about entity relationships, structural connections,
    multi-hop paths, or ontological classification.

``MIXED``
    Questions that require both sources.  When no strong signal is detected,
    the default is MIXED to avoid silently excluding a source.

Classification method
---------------------
Keyword/pattern-based (no LLM, no network calls).  Patterns are generic and
domain-neutral -- no domain-specific vocabulary is hard-coded here.  Domain
admins can supply a domain contract (from ``domain.yaml``) to produce richer,
domain-aware routing instructions.

Generated output
----------------
:func:`generate_routing_instructions` returns a Markdown string suitable for
pasting into the Fabric Data Agent ``Additional instructions`` field.
When a domain contract is supplied, the output uses domain-specific entity
types, competency questions, and business context rather than generic examples.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Routing category
# ---------------------------------------------------------------------------


class RouteCategory(Enum):
    """The routing category assigned to a question."""

    SEARCH = "search"
    GRAPH = "graph"
    MIXED = "mixed"


# ---------------------------------------------------------------------------
# Keyword sets used by the classifier
# These are domain-neutral structural and content indicators only.
# ---------------------------------------------------------------------------

# Strong indicators of GRAPH questions (structure / topology / relationships)
_GRAPH_KEYWORDS: list[str] = [
    r"\bhierarch(?:y|ies)\b",
    r"\bdependenc(?:y|ies)\b",
    r"\bdepend(?:s|ing|ed)? on\b",
    r"\btopolog(?:y|ies)\b",
    r"\brelationship",
    r"\brelated\s+to\b",
    r"\bhas[_\s]component",
    r"\bhas[_\s]part\b",
    r"\bentit(?:y|ies)\b",
    r"\bedge(?:s)?\b",
    r"\bgraph\b",
    r"\bnode(?:s)?\b",
    r"\bparent\b",
    r"\bchild(?:ren)?\b",
    r"\bancestor(?:s)?\b",
    r"\bdescendant(?:s)?\b",
    r"\bpath(?:s)?\s+(?:from|to|between)\b",
    r"\btravers(?:e|al|ing)\b",
    r"\bconnect(?:ed|ion|ions)\b",
    r"\blinked?\s+to\b",
    r"\bgql\b",
    r"\bcypher\b",
    r"\bmatch\s*\(",
    r"\bontolog(?:y|ies)\b",
    r"\bcomponent(?:s)?\b",
]

# Strong indicators of SEARCH questions (content / document / factual lookup)
_SEARCH_KEYWORDS: list[str] = [
    r"\bfind\s+documents?\b",
    r"\bsearch\s+for\b",
    r"\bfull\s+text\b",
    r"\bpart\s+number\b",
    r"\bserial\s+number\b",
    r"\btracking\s+number\b",
    r"\brecord\s+number\b",
    r"\bspecification",
    r"\binstruction(?:s)?\b",
    r"\bmanual\b",
    r"\bguide(?:lines?)?\b",
    r"\bdocument(?:s|ation)?\b",
    r"\bpassage\b",
    r"\bchunk\b",
    r"\bfaq\b",
    r"\bwarranty\b",
    r"\bwhat\s+is\s+the\s+(?:part|serial|model|tracking)\s+number\b",
    r"\bhow\s+(?:many|much)\b",
    r"\bwhen\s+(?:was|did|is)\b",
    r"\bwhere\s+(?:is|are|can)\b",
    r"\btext\s+of\b",
    r"\bverbatim\b",
    r"\bstep[_\s]by[_\s]step\b",
    r"\bsteps?\s+to\b",
    r"\bfull\s+instructions\b",
    r"\blatest\s+version\b",
    r"\brelease\s+notes\b",
    r"\bpolicy\b",
    r"\bregulation\b",
    r"\bcertif(?:ied|ication|icate)\b",
    r"\bapproved\s+(?:dosage|dose|treatment|protocol)\b",
    r"\bclinical\s+guideline",
    r"\bpatient\s+(?:id|record|number)\b",
    r"\border\s+(?:id|number|status)\b",
]

# Compile patterns once
_GRAPH_RE: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in _GRAPH_KEYWORDS
]
_SEARCH_RE: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in _SEARCH_KEYWORDS
]


# ---------------------------------------------------------------------------
# Routing result
# ---------------------------------------------------------------------------


@dataclass
class RoutingResult:
    """The routing decision for a single question."""

    question: str
    category: RouteCategory
    graph_signals: list[str] = field(default_factory=list)
    search_signals: list[str] = field(default_factory=list)
    rationale: str = ""


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify_question(question: str) -> RoutingResult:
    """Classify *question* into ``SEARCH``, ``GRAPH``, or ``MIXED``."""
    q = question.strip()
    graph_signals = [p.pattern for p in _GRAPH_RE if p.search(q)]
    search_signals = [p.pattern for p in _SEARCH_RE if p.search(q)]

    has_graph = bool(graph_signals)
    has_search = bool(search_signals)

    if has_graph and has_search:
        category = RouteCategory.MIXED
        rationale = (
            f"Question matches both GRAPH signals {graph_signals[:3]} "
            f"and SEARCH signals {search_signals[:3]}; routing to MIXED."
        )
    elif has_graph:
        category = RouteCategory.GRAPH
        rationale = f"Question matches GRAPH signals {graph_signals[:3]}."
    elif has_search:
        category = RouteCategory.SEARCH
        rationale = f"Question matches SEARCH signals {search_signals[:3]}."
    else:
        # Unknown -- conservative default: MIXED
        category = RouteCategory.MIXED
        rationale = "No strong signal detected; defaulting to MIXED to include both sources."

    logger.debug(
        "[routing] %r -- %s (graph=%d, search=%d)",
        q[:80],
        category.value,
        len(graph_signals),
        len(search_signals),
    )
    return RoutingResult(
        question=q,
        category=category,
        graph_signals=graph_signals,
        search_signals=search_signals,
        rationale=rationale,
    )


def routing_hints_for_question(question: str) -> RoutingResult:
    """Convenience alias for :func:`classify_question`."""
    return classify_question(question)


# ---------------------------------------------------------------------------
# Instruction generator
# ---------------------------------------------------------------------------


def generate_routing_instructions(
    *,
    search_source_names: Sequence[str] = (),
    graph_source_names: Sequence[str] = (),
    ontology_source_names: Sequence[str] = (),
    competency_questions: Sequence[str] = (),
    agent_name: str = "Data Agent",
    domain_contract: object | None = None,
) -> str:
    """Generate a Markdown routing-instruction block for a Fabric Data Agent.

    When a *domain_contract* (a ``DomainReview`` or compatible object with
    ``domain``, ``business``, ``problem``, and ``competency_questions`` attributes)
    is provided, the output includes domain-specific context and examples
    instead of generic placeholders.  This avoids hard-coding any
    domain-specific vocabulary in the template itself.

    Parameters
    ----------
    search_source_names : Sequence[str]
        Names of AI Search knowledge sources (for SEARCH routing).
    graph_source_names : Sequence[str]
        Names of graph / GQL data sources (for GRAPH routing).
    ontology_source_names : Sequence[str]
        Names of ontology data sources (for GRAPH routing).
    competency_questions : Sequence[str]
        Sample questions -- each is classified and rendered as a few-shot.
    agent_name : str
        Display name of the agent (used in the header).
    domain_contract : object | None
        Optional domain contract with domain-specific context.  When present,
        entity types, business context, and problem statements are used to
        enrich the instructions.  When ``None``, generic neutral text is used.

    Returns
    -------
    str
        Markdown instruction document.
    """
    search_srcs = list(search_source_names)
    graph_srcs = list(graph_source_names) + list(ontology_source_names)

    # Extract domain-specific context from contract if available
    domain_name: str = ""
    business_context: str = ""
    entity_type_names: list[str] = []
    domain_questions: list[str] = list(competency_questions)

    if domain_contract is not None:
        try:
            domain_name = str(getattr(domain_contract.domain, "name", "") or "")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        try:
            business_context = str(getattr(domain_contract.business, "organization_context", "") or "")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        try:
            entity_types = getattr(domain_contract, "entity_types", None) or []
            entity_type_names = [getattr(et, "name", str(et)) for et in entity_types]
        except Exception:  # noqa: BLE001
            pass
        try:
            dc_cqs = getattr(domain_contract, "competency_questions", None) or []
            if dc_cqs and not competency_questions:
                domain_questions = [getattr(cq, "question", str(cq)) for cq in dc_cqs]
        except Exception:  # noqa: BLE001
            pass

    lines: list[str] = []
    lines.append(f"# Routing instructions for `{agent_name}`")
    if domain_name:
        lines.append(f"*Domain: {domain_name}*")
    lines.append("")
    lines.append(
        "This agent has access to multiple data sources.  "
        "Use the routing rules below to decide which source to query."
    )
    if business_context:
        lines.append("")
        lines.append(f"**Business context**: {business_context}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # --- SEARCH rules ---
    lines.append("## When to use AI Search (document / factual lookup)")
    lines.append("")
    lines.append("Use the AI Search source(s) when the question asks for:")
    lines.append("- Specific field values (identifiers, codes, SKUs)")
    lines.append("- Full-text document content (policies, guides, specifications)")
    lines.append("- Verbatim instructions or structured metadata")
    lines.append("- Direct lookup queries referencing known attribute names")
    lines.append("")
    if search_srcs:
        lines.append(f"**Search sources**: {', '.join(f'`{s}`' for s in search_srcs)}")
        lines.append("")

    # --- GRAPH rules ---
    lines.append("## When to use the Knowledge Graph / Ontology")
    lines.append("")
    lines.append("Use the graph or ontology source(s) when the question asks for:")
    lines.append("- Entity hierarchies or parent/child relationships")
    lines.append("- Dependency or topology queries (what is connected to what)")
    if entity_type_names:
        lines.append(f"- Typed relationships between {', '.join(entity_type_names[:4])}")
    else:
        lines.append("- Typed relationships between domain entities")
    lines.append("- Path traversal or multi-hop graph patterns")
    lines.append("")
    if graph_srcs:
        lines.append(f"**Graph sources**: {', '.join(f'`{s}`' for s in graph_srcs)}")
        lines.append("")

    # --- MIXED rules ---
    lines.append("## When to use BOTH sources (mixed questions)")
    lines.append("")
    lines.append(
        "Use both sources when the question combines structural queries with content lookup."
    )
    lines.append(
        "Always query the graph first (for structure), then enrich with Search (for content)."
    )
    lines.append("")

    # --- Few-shots from questions ---
    if domain_questions:
        lines.append("---")
        lines.append("")
        lines.append("## Question routing examples (few-shots)")
        lines.append("")
        for q in domain_questions:
            result = classify_question(q)
            icon = {
                RouteCategory.SEARCH: "d",
                RouteCategory.GRAPH: "g",
                RouteCategory.MIXED: "m",
            }[result.category]
            lines.append(
                f"- [{icon}] **{result.category.value.upper()}** -- {q}"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "> Generated by `fabric-kg knowledge routing`. "
        "Re-generate after adding new data sources or competency questions."
    )
    lines.append("")
    return "\n".join(lines)
