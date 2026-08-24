"""Production semantic query and runtime diagnostic validation (SPEC-008A §9, §10).

Provides deterministic, structured-findings APIs for:
- physical GQL/Cypher query validation (§9.2): fences, terminal projection, optional paths
- persisted-schema query validation (S8A-QRY-002): labels, relationships, owner-scoped
  properties, endpoints/direction, bounded LIMIT, and normalized query hashing
- semantic query plan budget validation (§9.4)
- runtime diagnostic completeness and status consistency (§10.1, §10.2, §10.4)

The SemanticQueryPlan schema itself enforces budget at construction (hard reject).
The SemanticDiagnosticRecord schema itself enforces envelope fields and status
consistency (hard reject).  These functions provide a structured findings API for
tooling that needs to collect, log, or display violations without relying on
ValidationError propagation, and to validate PartialDiagnosticExport records.

``validate_physical_query`` remains fully backwards compatible: callers that do
not pass ``schema`` retain exactly the original fence/terminal-projection/
optional-path behavior.  Passing ``schema`` (a sealed ``PersistedQuerySchema``)
additionally enables strict label, relationship, owner-scoped property,
endpoint/direction, required-path, and bounded-LIMIT validation (S8A-QRY-002).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Mapping, Union

from .schemas import (
    PartialDiagnosticExport,
    PersistedQuerySchema,
    SemanticDiagnosticRecord,
    SemanticQueryPlan,
    compute_persisted_query_schema_hash,
)

if TYPE_CHECKING:  # pragma: no cover
    pass

# ---------------------------------------------------------------------------
# Finding type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryFinding:
    """One deterministic query or runtime validation failure."""

    code: str
    message: str


class SemanticQueryValidationError(ValueError):
    """Raised when query or runtime validation finds blocking invariant violations."""

    def __init__(self, findings: list[QueryFinding]) -> None:
        self.findings = tuple(findings)
        super().__init__(
            "; ".join(f"{f.code}: {f.message}" for f in findings)
        )


# ---------------------------------------------------------------------------
# Physical query validators (SPEC-008A §9.2)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```", re.DOTALL)
_TOP_LEVEL_CLAUSE_RE = re.compile(
    r"\bOPTIONAL\s+MATCH\b|\bMATCH\b|\bRETURN\b|\bWITH\b|\bCALL\b|"
    r"\bUNWIND\b|\bCREATE\b|\bMERGE\b|\bDELETE\b|\bSET\b|\bREMOVE\b|"
    r"\bUNION\b|\bLIMIT\b",
    re.IGNORECASE,
)
# Trailing node uses a non-consuming lookahead so that chained paths
# (a)-[:R1]->(b)-[:R2]->(c) resolve every hop: the shared middle node is not
# consumed by the first match, so finditer's next match starts on it.
# Both node labels are OPTIONAL (only the alias is captured when present):
# GQL/Cypher allows an alias bound to a label earlier in the query (in an
# outer/prior MATCH) to be reused without repeating the label, e.g.
# ``MATCH (m:MaintenanceAction) ... OPTIONAL MATCH (m)-[:R]->(p:Project)``.
# Callers resolve an empty ``left_type``/``right_type`` via the query-wide
# alias -> label map (``_collect_alias_labels``) built from every explicitly
# labeled occurrence of that alias, so a genuinely reused, now-unlabeled
# alias still matches correctly instead of being reported as path loss.
_PATH_PATTERN = re.compile(
    r"\(\s*(?P<left_alias>[A-Za-z_][A-Za-z0-9_]*)?\s*"
    r"(?::\s*`?(?P<left_type>[A-Za-z_][A-Za-z0-9_.-]*)`?)?[^)]*\)"
    r"\s*(?P<left_arrow><-|-)\s*"
    r"\[[^\]]*?:\s*`?(?P<relationship>[A-Za-z_][A-Za-z0-9_.-]*)`?[^\]]*\]"
    r"\s*(?P<right_arrow>->|-)\s*"
    r"(?=\(\s*(?P<right_alias>[A-Za-z_][A-Za-z0-9_]*)?\s*"
    r"(?::\s*`?(?P<right_type>[A-Za-z_][A-Za-z0-9_.-]*)`?)?[^)]*\))",
    re.IGNORECASE,
)
# Any node pattern with a label, e.g. (w:Widget) or (:Widget {id: 1}); used to
# validate every node label against the persisted schema, and to build the
# alias -> label map used for owner-scoped property validation.
_NODE_LABEL_PATTERN = re.compile(
    r"\(\s*(?P<alias>[A-Za-z_][A-Za-z0-9_]*)?\s*:\s*`?(?P<label>[A-Za-z_][A-Za-z0-9_.-]*)`?[^)]*\)",
    re.IGNORECASE,
)
# Any relationship pattern with a label, independent of endpoints; used to
# validate every relationship label against the persisted schema.
_RELATIONSHIP_LABEL_PATTERN = re.compile(
    r"\[\s*(?:[A-Za-z_][A-Za-z0-9_]*)?\s*:\s*`?(?P<label>[A-Za-z_][A-Za-z0-9_.-]*)`?[^\]]*\]",
    re.IGNORECASE,
)
_UNQUOTED_NODE_LABEL_PATTERN = re.compile(
    r"\(\s*(?:[A-Za-z_][A-Za-z0-9_]*)?\s*:\s*"
    r"(?!`)(?P<label>[A-Za-z_][A-Za-z0-9_.-]*)",
    re.IGNORECASE,
)
_UNQUOTED_RELATIONSHIP_LABEL_PATTERN = re.compile(
    r"\[\s*(?:[A-Za-z_][A-Za-z0-9_]*)?\s*:\s*"
    r"(?!`)(?P<label>[A-Za-z_][A-Za-z0-9_.-]*)",
    re.IGNORECASE,
)
# alias.property (or alias.`property`) token, used to validate owner-scoped
# property access against the label the alias was bound to.
_PROPERTY_ACCESS_PATTERN = re.compile(
    r"\b(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\."
    r"(?P<property>`(?:``|[^`])+`|[A-Za-z_][A-Za-z0-9_.-]*)"
)
_NODE_PROPERTY_MAP_PATTERN = re.compile(
    r"\(\s*(?P<alias>[A-Za-z_][A-Za-z0-9_]*)?\s*"
    r"(?::\s*`?(?P<label>[A-Za-z_][A-Za-z0-9_.-]*)`?)?\s*"
    r"\{(?P<properties>[^{}]*)\}[^)]*\)",
    re.IGNORECASE,
)
_INLINE_PROPERTY_KEY_PATTERN = re.compile(
    r"(?:^|,)\s*"
    r"(?P<property>`(?:``|[^`])+`|[A-Za-z_][A-Za-z0-9_.-]*)\s*:"
)
_LIMIT_VALUE_RE = re.compile(r"\bLIMIT\s+(?P<value>\d+)\b", re.IGNORECASE)
_VARIABLE_LENGTH_RELATIONSHIP_RE = re.compile(
    r"\[[^\]]*\*[^\]]*\]",
    re.IGNORECASE,
)
_RETURN_BODY_RE = re.compile(
    r"\bRETURN\b(?P<body>.*?)(?:\bLIMIT\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_RESERVED_ALIAS_WORDS = frozenset({
    "and", "or", "not", "xor", "true", "false", "null", "count", "sum",
    "avg", "min", "max", "collect", "distinct", "as", "when", "case",
    "then", "else", "end", "exists", "all", "any", "none", "single",
})
# Fabric Graph and Ontology-backed Data Agents treat these names as reserved
# physical identifiers. Queries can quote them, but published type names must
# be made non-reserved so every Fabric semantic surface can resolve them.
FABRIC_RESERVED_PHYSICAL_IDENTIFIERS = frozenset({"project"})


def _decode_identifier(token: str) -> str:
    """Return the physical identifier represented by a bare/backtick token."""
    if token.startswith("`") and token.endswith("`"):
        return token[1:-1].replace("``", "`")
    return token


def _mask_literals_and_comments(query: str, *, mask_backticks: bool = True) -> str:
    """Replace non-code text while preserving positions and line boundaries.

    By default, backtick-quoted text is masked exactly like a string
    literal (``mask_backticks=True``), which is correct for clause/depth
    parsing where only the *shape* of the query matters.  Backticks in GQL
    quote an *identifier* (a node label, relationship label, or property
    name), not a string literal, so callers that need to validate those
    identifiers against a persisted schema must pass
    ``mask_backticks=False`` to keep the backtick-quoted text intact (while
    comments and real string literals are still masked, so they are never
    misread as identifiers).
    """
    chars = list(query)
    index = 0
    state = "code"
    while index < len(chars):
        char = chars[index]
        next_char = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if char == "/" and next_char == "*":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if char == "`" and not mask_backticks:
                # Keep the backtick delimiter itself so downstream
                # identifier regexes (which expect an optional literal
                # backtick around labels/relationships/properties) still
                # match; only its *state* is tracked here.
                state = "backtick"
                index += 1
                continue
            if char in {"'", '"', "`"}:
                chars[index] = " "
                state = {"'": "single", '"': "double", "`": "backtick"}[char]
                index += 1
                continue
            index += 1
            continue
        if state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                chars[index] = " "
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "code"
            else:
                if char != "\n":
                    chars[index] = " "
                index += 1
            continue
        if state == "backtick" and not mask_backticks:
            # Preserve identifier text verbatim; only watch for the closing
            # backtick to return to the code state.
            if char == "`":
                state = "code"
            index += 1
            continue
        quote = {"single": "'", "double": '"', "backtick": "`"}[state]
        if char == "\\" and index + 1 < len(chars):
            chars[index] = chars[index + 1] = " "
            index += 2
            continue
        if char == quote:
            chars[index] = " "
            state = "code"
        elif char != "\n":
            chars[index] = " "
        index += 1
    return "".join(chars)


def _top_level_clauses(query: str) -> list[tuple[str, int, int]]:
    """Return top-level clause keywords, ignoring subqueries and literals."""
    masked = _mask_literals_and_comments(query)
    depth = 0
    depth_at: list[int] = [0] * (len(masked) + 1)
    for index, char in enumerate(masked):
        depth_at[index] = depth
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
    clauses: list[tuple[str, int, int]] = []
    for match in _TOP_LEVEL_CLAUSE_RE.finditer(masked):
        if depth_at[match.start()] == 0:
            keyword = re.sub(r"\s+", " ", match.group(0).upper())
            clauses.append((keyword, match.start(), match.end()))
    return clauses


def _relationship_token(semantic_id: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", semantic_id.rsplit(":", 1)[-1].upper())


def _collect_alias_labels(query: str) -> dict[str, str]:
    """Build a query-wide alias -> label map from every explicitly labeled node.

    GQL/Cypher lets an alias bound to a label in one clause (e.g. an earlier
    MATCH) be reused without repeating the label in a later clause, e.g.
    ``MATCH (m:MaintenanceAction) ... OPTIONAL MATCH (m)-[:R]->(p:Project)``.
    Hop/path validation resolves such a reused, now-unlabeled alias through
    its earlier binding (this map) rather than treating the hop as having no
    resolvable endpoint.  Scans the *entire* query, not just the current
    clause, since the binding may come from a prior clause.
    """
    identifier_masked = _mask_literals_and_comments(query, mask_backticks=False)
    alias_labels: dict[str, str] = {}
    for match in _NODE_LABEL_PATTERN.finditer(identifier_masked):
        alias = match.group("alias")
        label = match.group("label")
        if alias and label and alias.lower() not in _RESERVED_ALIAS_WORDS:
            alias_labels.setdefault(alias, label)
    return alias_labels


def _collect_matched_hops(
    query: str,
    keywords: frozenset[str],
) -> list[tuple[str, str, str, str]]:
    """Collect (from_token, relationship_token, to_token, direction) tuples.

    Scans every top-level clause whose keyword is in ``keywords`` (e.g.
    ``{"MATCH"}`` or ``{"OPTIONAL MATCH"}``) for typed node-relationship-node
    hops.  Chained paths such as ``(a)-[:R1]->(b)-[:R2]->(c)`` resolve to one
    tuple per hop because ``_PATH_PATTERN`` uses a non-consuming lookahead for
    the trailing node.  A node with no label of its own (an alias reused from
    an earlier clause, e.g. ``(m)`` after ``MATCH (m:MaintenanceAction)``) is
    resolved via the query-wide alias -> label map instead of being treated
    as unresolvable.
    """
    alias_labels = _collect_alias_labels(query)
    clauses = _top_level_clauses(query)
    hops: list[tuple[str, str, str, str]] = []
    for index, (keyword, start, _) in enumerate(clauses):
        if keyword not in keywords:
            continue
        end = clauses[index + 1][1] if index + 1 < len(clauses) else len(query)
        clause_text = query[start:end]
        for match in _PATH_PATTERN.finditer(clause_text):
            direction = (
                "source_to_target"
                if (
                    match.group("left_arrow") == "-"
                    and match.group("right_arrow") == "->"
                )
                else "target_to_source"
            )
            left_type = match.group("left_type") or alias_labels.get(
                match.group("left_alias") or "", ""
            )
            right_type = match.group("right_type") or alias_labels.get(
                match.group("right_alias") or "", ""
            )
            hops.append((
                _relationship_token(left_type),
                _relationship_token(match.group("relationship")),
                _relationship_token(right_type),
                direction,
            ))
    return hops


def _find_matching_hop(
    hops: list[tuple[str, str, str, str]],
    unused: set[int],
    *,
    relationship_token: str,
    from_token: str,
    to_token: str,
    step_direction: str,
) -> int | None:
    """Return the index of the first unused hop matching the step, if any."""
    for path_index in sorted(unused):
        left, relationship, right, direction = hops[path_index]
        if relationship != relationship_token:
            continue
        if step_direction == "source_to_target":
            matches = (
                left == from_token and right == to_token
                and direction == "source_to_target"
            ) or (
                left == to_token and right == from_token
                and direction == "target_to_source"
            )
        else:
            matches = (
                left == from_token and right == to_token
                and direction == "target_to_source"
            ) or (
                left == to_token and right == from_token
                and direction == "source_to_target"
            )
        if matches:
            return path_index
    return None


def _normalize_query_code_whitespace(query: str) -> str:
    """Collapse formatting whitespace without changing quoted content."""
    normalized: list[str] = []
    state = "code"
    pending_space = False
    index = 0

    def flush_space() -> None:
        nonlocal pending_space
        if pending_space and normalized and normalized[-1] != " ":
            normalized.append(" ")
        pending_space = False

    while index < len(query):
        char = query[index]
        next_char = query[index + 1] if index + 1 < len(query) else ""
        if state == "code":
            if char.isspace():
                pending_space = True
                index += 1
                continue
            flush_space()
            if char == "/" and next_char == "/":
                normalized.extend((char, next_char))
                index += 2
                state = "line_comment"
                continue
            if char == "/" and next_char == "*":
                normalized.extend((char, next_char))
                index += 2
                state = "block_comment"
                continue
            normalized.append(char)
            if char == "'":
                state = "single_quote"
            elif char == '"':
                state = "double_quote"
            elif char == "`":
                state = "backtick"
            index += 1
            continue

        normalized.append(char)
        if state in {"single_quote", "double_quote"} and char == "\\":
            if index + 1 < len(query):
                normalized.append(query[index + 1])
                index += 2
                continue
        if state == "single_quote" and char == "'":
            state = "code"
        elif state == "double_quote" and char == '"':
            state = "code"
        elif state == "backtick" and char == "`":
            if next_char == "`":
                normalized.append(next_char)
                index += 2
                continue
            state = "code"
        elif state == "line_comment" and char == "\n":
            state = "code"
        elif state == "block_comment" and char == "*" and next_char == "/":
            normalized.append(next_char)
            index += 2
            state = "code"
            continue
        index += 1

    return "".join(normalized).strip()


def compute_physical_query_hash(query: str) -> str:
    """Compute a normalized, deterministic sha256 hash of a physical query.

    Formatting whitespace outside quoted content is collapsed, while string
    literals, quoted identifiers, and comments are retained exactly. This
    keeps formatting-only edits stable without allowing distinct literal
    values such as ``'A  B'`` and ``'A B'`` to share a receipt hash.
    """
    normalized = _normalize_query_code_whitespace(query)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_physical_query(
    query: str,
    plan: SemanticQueryPlan | None = None,
    *,
    relationship_labels: Mapping[str, str] | None = None,
    type_labels: Mapping[str, str] | None = None,
    schema: PersistedQuerySchema | None = None,
    raise_on_findings: bool = False,
) -> list[QueryFinding]:
    """Validate a physical GQL/Cypher query string for SPEC-008A §9.2 invariants.

    Checks always performed (backwards compatible with all existing callers):
    - no markdown code fences (```) in the query text
    - query contains a top-level terminal RETURN clause (a trailing top-level
      LIMIT after RETURN is still terminal)
    - optional path steps from the plan are realized as OPTIONAL MATCH in the
      query (only checked when ``plan`` is provided)

    Additional checks performed only when ``schema`` (a sealed
    PersistedQuerySchema) is supplied (S8A-QRY-002):
    - every node label referenced in the query is known to the schema
    - every relationship label referenced in the query is known to the schema
    - every relationship's endpoints/direction match the schema
    - every owner-scoped property access (``alias.property``) belongs to the
      schema node the alias was bound to
    - every required (non-optional) path step from ``plan`` is realized as a
      non-optional MATCH (only checked when ``plan`` is also provided)
    - a bounded top-level LIMIT is present and does not exceed
      ``plan.budget.max_rows_per_subquery`` (only checked when ``plan`` is
      also provided)

    Args:
        query: The physical query string to validate.
        plan: The SemanticQueryPlan the query was generated from.  When ``None``
              the optional-MATCH plan-alignment check is skipped, enabling callers
              (e.g. the executor) that only have the physical query string to still
              enforce FENCE and RETURN invariants.
        relationship_labels: Optional map of semantic relationship ID -> physical
              label, used to resolve the optional-path-loss check.
        type_labels: Optional map of semantic type ID -> physical label, used to
              resolve the optional-path-loss check.
        schema: Optional sealed PersistedQuerySchema.  When provided, enables the
              full S8A-QRY-002 strict validation described above.  When ``None``,
              behavior is identical to callers that predate S8A-QRY-002.
        raise_on_findings: If True, raise SemanticQueryValidationError on findings.

    Returns:
        List of QueryFinding instances.  Empty when all invariants pass.
    """
    findings: list[QueryFinding] = []

    # §9.2: no markdown code fences
    if _FENCE_RE.search(query):
        findings.append(QueryFinding(
            "QUERY_FENCED",
            "Physical query contains markdown code fences (```). "
            "Fences must be removed before execution; a fenced query will "
            "fail at parse time in the GQL execution layer.",
        ))

    # §9.2: terminal projection required
    stripped = query.strip()
    if not stripped:
        findings.append(QueryFinding(
            "QUERY_EMPTY",
            "Query is empty; no terminal projection possible.",
        ))
    else:
        clauses = _top_level_clauses(stripped)
        top_level_returns = [
            clause for clause in clauses if clause[0] == "RETURN"
        ]
        # A trailing top-level LIMIT after RETURN is still a terminal
        # projection (e.g. "RETURN x LIMIT 10"); only non-LIMIT clauses are
        # considered when checking for a terminal RETURN.
        projection_clauses = [clause for clause in clauses if clause[0] != "LIMIT"]
        if (
            not top_level_returns
            or not projection_clauses
            or projection_clauses[-1][0] != "RETURN"
        ):
            findings.append(QueryFinding(
                "QUERY_NO_TERMINAL_PROJECTION",
                "Query lacks a top-level terminal RETURN clause. RETURN inside "
                "a string, comment, or nested subquery is not a terminal "
                "projection for the outer query.",
            ))

    # Effective physical label maps used by both the optional-path-loss
    # check (below) and the schema-aware required-path-realization check
    # (S8A-QRY-002, further down): when a sealed PersistedQuerySchema is
    # supplied, its node/relationship labels are the ground truth physical
    # labels, so path matching must resolve semantic IDs through *them*
    # rather than through the semantic ID's own tail token (which may differ
    # from the actual physical label).  Explicit ``type_labels``/
    # ``relationship_labels`` arguments still take precedence over the
    # schema when both are supplied.
    schema_type_labels: dict[str, str] = (
        {node.semantic_id: node.label for node in schema.nodes if node.label}
        if schema is not None
        else {}
    )
    schema_relationship_labels: dict[str, str] = (
        {rel.semantic_id: rel.label for rel in schema.relationships if rel.label}
        if schema is not None
        else {}
    )
    effective_type_labels: dict[str, str] = {
        **schema_type_labels, **(dict(type_labels) if type_labels else {})
    }
    effective_relationship_labels: dict[str, str] = {
        **schema_relationship_labels,
        **(dict(relationship_labels) if relationship_labels else {}),
    }

    # §9.3: every optional path step must be emitted as OPTIONAL MATCH.
    if plan is not None:
        optional_steps = [step for step in plan.path_steps if step.optional]
        if optional_steps:
            optional_hops = _collect_matched_hops(
                query, frozenset({"OPTIONAL MATCH"})
            )
            unused_paths = set(range(len(optional_hops)))
            missing_steps: list[str] = []
            for step in optional_steps:
                relationship_token = _relationship_token(
                    effective_relationship_labels.get(step.via_relationship_id)
                    or step.via_relationship_id
                )
                from_token = _relationship_token(
                    effective_type_labels.get(step.from_type_id)
                    or step.from_type_id
                )
                to_token = _relationship_token(
                    effective_type_labels.get(step.to_type_id)
                    or step.to_type_id
                )
                matched_index = _find_matching_hop(
                    optional_hops,
                    unused_paths,
                    relationship_token=relationship_token,
                    from_token=from_token,
                    to_token=to_token,
                    step_direction=step.direction,
                )
                if matched_index is None:
                    missing_steps.append(step.step_id)
                else:
                    unused_paths.remove(matched_index)
            if missing_steps:
                findings.append(QueryFinding(
                    "QUERY_OPTIONAL_PATH_LOSS",
                    "Physical query does not preserve every optional semantic "
                    f"path step as OPTIONAL MATCH. Missing steps: {missing_steps}.",
                ))

    # S8A-QRY-002: strict persisted-schema validation.  Only runs when a
    # sealed PersistedQuerySchema is supplied; existing callers without a
    # schema retain exactly the behavior above.
    if schema is not None:
        # ``mask_backticks=False``: backtick-quoted labels/relationships/
        # properties are *identifiers*, not string literals — they must
        # remain intact here so the schema-validation regexes below can
        # actually see and validate them (comments and real string/quote
        # literals are still masked).
        identifier_masked = _mask_literals_and_comments(
            query, mask_backticks=False
        )
        if (
            schema.schema_mode == "schema2_bounded"
            and _VARIABLE_LENGTH_RELATIONSHIP_RE.search(identifier_masked)
        ):
            findings.append(QueryFinding(
                "QUERY_VARIABLE_LENGTH_TRAVERSAL",
                "Schema-2 queries cannot use variable-length relationship "
                "traversal; every hop must be explicit.",
            ))

        node_label_tokens: set[str] = set()
        alias_to_label: dict[str, str] = {}
        for match in _NODE_LABEL_PATTERN.finditer(identifier_masked):
            label = match.group("label")
            node_label_tokens.add(label)
            alias = match.group("alias")
            if alias and alias.lower() not in _RESERVED_ALIAS_WORDS:
                alias_to_label[alias] = label
        relationship_aliases = {
            match.group(1)
            for match in re.finditer(
                r"\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*:",
                identifier_masked,
            )
        }
        if schema.schema_mode == "schema2_bounded":
            for return_match in _RETURN_BODY_RE.finditer(identifier_masked):
                expressions = [
                    expression.strip()
                    for expression in return_match.group("body").split(",")
                ]
                whole_values = sorted({
                    expression
                    for expression in expressions
                    if expression in alias_to_label
                    or expression in relationship_aliases
                    or re.fullmatch(
                        r"TO_JSON_STRING\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\)",
                        expression,
                        re.IGNORECASE,
                    )
                })
                if whole_values:
                    findings.append(QueryFinding(
                        "QUERY_WHOLE_GRAPH_VALUE_RETURN",
                        "Schema-2 queries may return approved scalar properties "
                        f"only, not whole nodes/edges: {whole_values}.",
                    ))

        relationship_label_tokens: set[str] = {
            match.group("label")
            for match in _RELATIONSHIP_LABEL_PATTERN.finditer(identifier_masked)
        }

        known_node_labels = {node.label for node in schema.nodes if node.label}
        known_relationship_labels = {
            rel.label for rel in schema.relationships if rel.label
        }

        unquoted_reserved = sorted({
            match.group("label")
            for pattern in (
                _UNQUOTED_NODE_LABEL_PATTERN,
                _UNQUOTED_RELATIONSHIP_LABEL_PATTERN,
            )
            for match in pattern.finditer(identifier_masked)
            if match.group("label").casefold()
            in FABRIC_RESERVED_PHYSICAL_IDENTIFIERS
        })
        if unquoted_reserved:
            findings.append(QueryFinding(
                "QUERY_RESERVED_IDENTIFIER_UNQUOTED",
                "Fabric GQL requires reserved physical identifier(s) to be "
                f"backtick-quoted: {unquoted_reserved}.",
            ))

        unknown_node_labels = sorted(node_label_tokens - known_node_labels)
        if unknown_node_labels:
            findings.append(QueryFinding(
                "QUERY_UNKNOWN_NODE_LABEL",
                "Physical query references node label(s) not present in the "
                f"persisted query schema: {unknown_node_labels}.",
            ))

        unknown_relationship_labels = sorted(
            relationship_label_tokens - known_relationship_labels
        )
        if unknown_relationship_labels:
            findings.append(QueryFinding(
                "QUERY_UNKNOWN_RELATIONSHIP_LABEL",
                "Physical query references relationship label(s) not present "
                f"in the persisted query schema: {unknown_relationship_labels}.",
            ))

        # Endpoints/direction for every hop across every MATCH/OPTIONAL MATCH.
        rel_by_label: dict[str, object] = {}
        for rel in schema.relationships:
            if rel.label:
                rel_by_label.setdefault(rel.label, rel)
        clauses_all = _top_level_clauses(query)
        endpoint_mismatches: list[str] = []
        for index, (keyword, start, _) in enumerate(clauses_all):
            if keyword not in {"MATCH", "OPTIONAL MATCH"}:
                continue
            end = (
                clauses_all[index + 1][1]
                if index + 1 < len(clauses_all)
                else len(query)
            )
            clause_text = query[start:end]
            for match in _PATH_PATTERN.finditer(clause_text):
                rel_label = match.group("relationship")
                rel_schema = rel_by_label.get(rel_label)
                if rel_schema is None:
                    continue  # already reported as an unknown relationship label
                # A node with no label of its own is an alias reused from an
                # earlier clause (e.g. ``(m)`` after ``MATCH
                # (m:MaintenanceAction)``); resolve it via the query-wide
                # alias -> label map instead of treating it as unresolvable.
                left_label = match.group("left_type") or alias_to_label.get(
                    match.group("left_alias") or ""
                )
                right_label = match.group("right_type") or alias_to_label.get(
                    match.group("right_alias") or ""
                )
                if left_label is None or right_label is None:
                    # Genuinely unresolvable endpoint (never labeled anywhere
                    # in the query) - not a reportable endpoint mismatch.
                    continue
                forward = (
                    match.group("left_arrow") == "-"
                    and match.group("right_arrow") == "->"
                )
                from_label, to_label = (
                    (left_label, right_label) if forward else (right_label, left_label)
                )
                # Relationship direction is always canonical source -> target
                # (see PersistedQueryRelationshipSchema.direction); the
                # physical query must realize that exact direction, not just
                # touch the same pair of labels in either order.
                endpoint_ok = (
                    from_label == rel_schema.source_label
                    and to_label == rel_schema.target_label
                )
                if not endpoint_ok:
                    endpoint_mismatches.append(f"{rel_label}: {from_label}->{to_label}")
        if endpoint_mismatches:
            findings.append(QueryFinding(
                "QUERY_RELATIONSHIP_ENDPOINT_MISMATCH",
                "Physical query relationship endpoint(s)/direction do not "
                "match the persisted query schema: "
                f"{sorted(set(endpoint_mismatches))}.",
            ))

        # Owner-scoped property validation: both alias.property expressions
        # and inline node maps such as ``(w:Widget {status: 'active'})`` must
        # use a physical key owned by the bound node type.
        node_by_label = {node.label: node for node in schema.nodes if node.label}
        unknown_properties: list[str] = []
        for match in _PROPERTY_ACCESS_PATTERN.finditer(identifier_masked):
            alias = match.group("alias")
            prop = _decode_identifier(match.group("property"))
            label = alias_to_label.get(alias)
            if label is None:
                continue
            node = node_by_label.get(label)
            if node is None:
                continue
            if prop not in node.physical_property_keys:
                unknown_properties.append(f"{alias}.{prop}")
        for match in _NODE_PROPERTY_MAP_PATTERN.finditer(identifier_masked):
            alias = match.group("alias") or ""
            label = match.group("label") or alias_to_label.get(alias)
            if label is None:
                continue
            node = node_by_label.get(label)
            if node is None:
                continue
            owner = alias or label
            for property_match in _INLINE_PROPERTY_KEY_PATTERN.finditer(
                match.group("properties")
            ):
                prop = _decode_identifier(property_match.group("property"))
                if prop not in node.physical_property_keys:
                    unknown_properties.append(f"{owner}.{prop}")
        if unknown_properties:
            findings.append(QueryFinding(
                "QUERY_UNKNOWN_PROPERTY",
                "Physical query references propert(y/ies) not owned by the "
                f"bound node type in the persisted query schema: "
                f"{sorted(set(unknown_properties))}.",
            ))

        if plan is not None:
            # Required paths must be realized as a non-optional MATCH.
            # Node/relationship tokens are resolved through the effective
            # physical label maps (schema-derived, overridable by explicit
            # relationship_labels/type_labels), not the raw semantic-ID tail,
            # so a physical label that differs from its canonical ID's tail
            # token (e.g. "entity-type:widget" projected as "PhysicalUnit")
            # still matches correctly.
            required_steps = [s for s in plan.path_steps if not s.optional]
            if required_steps:
                required_hops = _collect_matched_hops(query, frozenset({"MATCH"}))
                unused_required = set(range(len(required_hops)))
                missing_required: list[str] = []
                for step in required_steps:
                    relationship_token = _relationship_token(
                        effective_relationship_labels.get(step.via_relationship_id)
                        or step.via_relationship_id
                    )
                    from_token = _relationship_token(
                        effective_type_labels.get(step.from_type_id)
                        or step.from_type_id
                    )
                    to_token = _relationship_token(
                        effective_type_labels.get(step.to_type_id)
                        or step.to_type_id
                    )
                    matched_index = _find_matching_hop(
                        required_hops,
                        unused_required,
                        relationship_token=relationship_token,
                        from_token=from_token,
                        to_token=to_token,
                        step_direction=step.direction,
                    )
                    if matched_index is None:
                        missing_required.append(step.step_id)
                    else:
                        unused_required.remove(matched_index)
                if missing_required:
                    findings.append(QueryFinding(
                        "QUERY_REQUIRED_PATH_MISSING",
                        "Physical query does not realize every required "
                        "semantic path step as a non-optional MATCH. Missing "
                        f"steps: {missing_required}.",
                    ))
                if unused_required:
                    findings.append(QueryFinding(
                        "QUERY_UNPLANNED_PATH",
                        "Physical query contains Graph hops absent from the "
                        "validated structured plan.",
                    ))

            # Bounded top-level LIMIT <= plan budget.
            limit_matches = [
                (int(match.group("value")), match.start())
                for match in _LIMIT_VALUE_RE.finditer(identifier_masked)
            ]
            top_level_limit_starts = {
                start for keyword, start, _ in clauses_all if keyword == "LIMIT"
            }
            top_level_limits = [
                value for value, start in limit_matches if start in top_level_limit_starts
            ]
            if not top_level_limits:
                findings.append(QueryFinding(
                    "QUERY_LIMIT_MISSING",
                    "Physical query has no top-level LIMIT clause; a bounded "
                    "result limit is required (SPEC-008A §9.4).",
                ))
            else:
                max_rows = plan.budget.max_rows_per_subquery
                enforced_max_rows = (
                    min(max_rows, 100)
                    if schema.schema_mode == "schema2_bounded"
                    else max_rows
                )
                over_budget = [
                    v for v in top_level_limits if v > enforced_max_rows
                ]
                if over_budget:
                    findings.append(QueryFinding(
                        "QUERY_LIMIT_OVER_BUDGET",
                        f"Physical query top-level LIMIT {over_budget} exceeds "
                        f"budget.max_rows_per_subquery={max_rows}.",
                    ))

    if raise_on_findings and findings:
        raise SemanticQueryValidationError(findings)
    return findings


# ---------------------------------------------------------------------------
# Semantic query plan validator (SPEC-008A §9.4)
# ---------------------------------------------------------------------------

def validate_query_plan(
    plan: SemanticQueryPlan,
    *,
    raise_on_findings: bool = False,
) -> list[QueryFinding]:
    """Validate a SemanticQueryPlan for complexity budget compliance (§9.4).

    Note: The SemanticQueryPlan schema enforces budget at construction time
    (raises ValidationError).  This function provides a structured-findings
    API for tooling that needs to collect or display budget violations
    independently of schema-level enforcement.

    Args:
        plan: The SemanticQueryPlan to validate.
        raise_on_findings: If True, raise SemanticQueryValidationError on findings.

    Returns:
        List of QueryFinding instances.  Empty when all invariants pass.
    """
    findings: list[QueryFinding] = []

    required_hops = sum(
        step.max_depth for step in plan.path_steps if not step.optional
    )
    if required_hops > plan.budget.max_hops:
        findings.append(QueryFinding(
            "PLAN_OVER_HOP_BUDGET",
            f"Plan has {required_hops} required bounded hop(s) but "
            f"budget.max_hops={plan.budget.max_hops}.",
        ))

    node_references = set(plan.required_types)
    for step in plan.path_steps:
        node_references.add(step.from_type_id)
        node_references.add(step.to_type_id)
    if len(node_references) > plan.budget.max_nodes:
        findings.append(QueryFinding(
            "PLAN_OVER_NODE_BUDGET",
            f"Plan references {len(node_references)} node type(s) but "
            f"budget.max_nodes={plan.budget.max_nodes}.",
        ))

    relationship_references = (
        set(plan.required_relationships)
        | set(plan.optional_relationships)
        | {step.via_relationship_id for step in plan.path_steps}
    )
    total_rels = len(relationship_references)
    if total_rels > plan.budget.max_relationships:
        findings.append(QueryFinding(
            "PLAN_OVER_RELATIONSHIP_BUDGET",
            f"Plan declares {total_rels} relationship(s) but "
            f"budget.max_relationships={plan.budget.max_relationships}.",
        ))

    if raise_on_findings and findings:
        raise SemanticQueryValidationError(findings)
    return findings


# ---------------------------------------------------------------------------
# Persisted-schema query plan resolver (S8A-QRY-002)
# ---------------------------------------------------------------------------

def resolve_query_plan(
    plan: SemanticQueryPlan,
    schema: PersistedQuerySchema,
    *,
    raise_on_findings: bool = False,
) -> list[QueryFinding]:
    """Validate a SemanticQueryPlan against a sealed PersistedQuerySchema.

    This is the plan-level counterpart to the ``schema=`` argument of
    ``validate_physical_query``: it validates the *semantic* plan before any
    physical query is generated from it, so an over-broad or stale plan is
    rejected as early as possible (SPEC-008A §9.1, §9.2).

    Checks:
    - the schema is sealed (has a non-empty ``schema_hash``) AND that hash
      matches the schema's recomputed content hash (detects a mutated or
      corrupted schema, not merely an unsealed one)
    - the plan declares a manifest identity that matches the schema's
      ``manifest_hash``
    - every required type is known to the schema and physically projected
      (has a non-empty graph label)
    - every required/optional relationship ID is known to the schema and
      physically projected (relationship label and both endpoint labels)
    - every requested property resolves against the owner-scoped
      ``owner_properties`` mapping (canonical property ID or physical key)
      of at least one of the plan's required types
    - every path step's endpoints and direction resolve against the schema's
      directed relationship definition

    Args:
        plan: The SemanticQueryPlan to validate.
        schema: The sealed PersistedQuerySchema to validate against.
        raise_on_findings: If True, raise SemanticQueryValidationError on findings.

    Returns:
        List of QueryFinding instances.  Empty when the plan resolves cleanly.
    """
    findings: list[QueryFinding] = []

    if not schema.schema_hash:
        findings.append(QueryFinding(
            "SCHEMA_UNSEALED",
            "PersistedQuerySchema has no schema_hash; only a sealed schema "
            "may be used to resolve a SemanticQueryPlan.",
        ))
    elif schema.schema_hash != compute_persisted_query_schema_hash(schema):
        findings.append(QueryFinding(
            "SCHEMA_HASH_MISMATCH",
            "PersistedQuerySchema.schema_hash does not match its recomputed "
            "content hash; the schema was mutated after sealing (or "
            "corrupted in transit) and cannot be trusted to resolve a plan.",
        ))

    if not plan.manifest_hash:
        findings.append(QueryFinding(
            "PLAN_MANIFEST_HASH_MISSING",
            "SemanticQueryPlan.manifest_hash is empty; a plan must declare "
            "the manifest identity it was compiled against before it can be "
            "resolved against a persisted query schema.",
        ))
    elif plan.manifest_hash != schema.manifest_hash:
        findings.append(QueryFinding(
            "PLAN_MANIFEST_MISMATCH",
            f"SemanticQueryPlan.manifest_hash '{plan.manifest_hash}' does not "
            f"match PersistedQuerySchema.manifest_hash '{schema.manifest_hash}'. "
            "The plan was not compiled against this persisted schema.",
        ))

    nodes_by_id = {node.semantic_id: node for node in schema.nodes}
    rels_by_id = {rel.semantic_id: rel for rel in schema.relationships}

    for type_id in plan.required_types:
        node = nodes_by_id.get(type_id)
        if node is None:
            findings.append(QueryFinding(
                "PLAN_UNKNOWN_TYPE",
                f"Required type '{type_id}' is not present in the persisted "
                "query schema.",
            ))
        elif not node.label:
            findings.append(QueryFinding(
                "PLAN_TYPE_NOT_PROJECTED",
                f"Required type '{type_id}' has no physical graph projection "
                "in the persisted query schema.",
            ))

    all_relationship_ids = sorted(
        set(plan.required_relationships) | set(plan.optional_relationships)
    )
    for rel_id in all_relationship_ids:
        rel = rels_by_id.get(rel_id)
        if rel is None:
            findings.append(QueryFinding(
                "PLAN_UNKNOWN_RELATIONSHIP",
                f"Relationship '{rel_id}' is not present in the persisted "
                "query schema.",
            ))
        elif not rel.label or not rel.source_label or not rel.target_label:
            findings.append(QueryFinding(
                "PLAN_RELATIONSHIP_NOT_PROJECTED",
                f"Relationship '{rel_id}' has no complete physical graph "
                "projection (label and both endpoint labels required) in "
                "the persisted query schema.",
            ))

    # Owner-scoped property resolution: a requested property may be either
    # the canonical property ID (preferred, e.g. a ManifestPropertyEntry
    # .property_id) or, for backwards compatibility with callers that still
    # request by physical graph property key, the physical key itself.
    owner_scoped_properties: set[str] = set()
    for type_id in plan.required_types:
        node = nodes_by_id.get(type_id)
        if node is not None:
            owner_scoped_properties.update(node.owner_properties.keys())
            owner_scoped_properties.update(node.physical_property_keys)
    for prop_name in plan.requested_properties:
        if prop_name not in owner_scoped_properties:
            findings.append(QueryFinding(
                "PLAN_UNKNOWN_PROPERTY",
                f"Requested property '{prop_name}' is not an owner-scoped "
                "graph property (by canonical property ID or physical key) "
                "of any required type in this plan.",
            ))

    for step in plan.path_steps:
        rel = rels_by_id.get(step.via_relationship_id)
        if rel is None:
            # Already reported above (unknown relationship); endpoints cannot
            # be resolved without a known relationship definition.
            continue
        if step.direction == "source_to_target":
            expected_from, expected_to = rel.source_type_id, rel.target_type_id
        else:
            expected_from, expected_to = rel.target_type_id, rel.source_type_id
        if step.from_type_id != expected_from or step.to_type_id != expected_to:
            findings.append(QueryFinding(
                "PLAN_PATH_ENDPOINT_MISMATCH",
                f"Path step '{step.step_id}' declares "
                f"{step.from_type_id} -> {step.to_type_id} "
                f"(direction={step.direction}) but relationship "
                f"'{step.via_relationship_id}' resolves "
                f"{rel.source_type_id} -> {rel.target_type_id} "
                "(source_to_target) in the persisted query schema.",
            ))

    if plan.schema_mode == "schema2_bounded":
        node_sequence = (
            [plan.path_steps[0].from_type_id]
            + [step.to_type_id for step in plan.path_steps]
            if plan.path_steps
            else []
        )
        for output in plan.outputs:
            if output.owner_kind == "node":
                if output.owner_index >= len(node_sequence):
                    findings.append(QueryFinding(
                        "PLAN_OUTPUT_OWNER_MISMATCH",
                        f"Output '{output.alias}' references missing node index "
                        f"{output.owner_index}.",
                    ))
                    continue
                owner_id = node_sequence[output.owner_index]
                node = nodes_by_id.get(owner_id)
                if (
                    node is None
                    or output.semantic_id != owner_id
                    or (
                        output.purpose == "id"
                        and output.property_name != node.id_property
                    )
                    or (
                        output.purpose == "display"
                        and output.property_name != node.display_property
                    )
                    or output.purpose not in {"id", "display"}
                ):
                    findings.append(QueryFinding(
                        "PLAN_OUTPUT_PROPERTY_MISMATCH",
                        f"Output '{output.alias}' is not an approved scalar "
                        f"property for node '{owner_id}'.",
                    ))
            else:
                if output.owner_index >= len(plan.path_steps):
                    findings.append(QueryFinding(
                        "PLAN_OUTPUT_OWNER_MISMATCH",
                        f"Output '{output.alias}' references missing relationship "
                        f"index {output.owner_index}.",
                    ))
                    continue
                step = plan.path_steps[output.owner_index]
                rel = rels_by_id.get(step.via_relationship_id)
                if (
                    rel is None
                    or output.semantic_id != step.via_relationship_id
                    or output.property_name != rel.evidence_property
                    or output.purpose != "evidence"
                ):
                    findings.append(QueryFinding(
                        "PLAN_OUTPUT_PROPERTY_MISMATCH",
                        f"Output '{output.alias}' is not the approved evidence "
                        f"property for relationship '{step.via_relationship_id}'.",
                    ))

    if raise_on_findings and findings:
        raise SemanticQueryValidationError(findings)
    return findings


# ---------------------------------------------------------------------------
# Runtime diagnostic validators (SPEC-008A §10.1, §10.2, §10.4)
# ---------------------------------------------------------------------------

_SOURCE_FAILURE_CATEGORIES: frozenset[str] = frozenset({
    "invalid_semantic_plan",
    "invalid_physical_query",
    "authorization_failure",
    "platform_failure",
    "timeout",
})

_REQUIRED_DIAGNOSTIC_FIELDS: frozenset[str] = frozenset({
    "schema_mode",
    "export_freshness_watermark",
    "partial_snapshot",
    "overlapping_snapshot",
    "workspace_id",
    "target_item_id",
    "semantic_contract_hash",
    "manifest_hash",
    "ontology_projection_hash",
    "graph_projection_hash",
    "search_projection_hash",
    "instruction_hash",
    "source_selection_hash",
    "query_schema_hash",
    "route",
    "selected_source",
    "semantic_plan",
    "semantic_plan_hash",
    "actual_hop_count",
    "physical_query_hash",
    "static_validation_passed",
    "query_row_count",
    "result_category",
    "error_category",
    "request_id",
    "correlation_id",
    "thread_id",
    "run_id",
    "operation_id",
    "latency_ms",
    "retry_count",
    "evidence_ids",
    "final_semantic_status",
})


def validate_diagnostic_record(
    record: Union[SemanticDiagnosticRecord, PartialDiagnosticExport],
    *,
    reference_watermark: str | None = None,
    max_age_hours: int = 24,
    raise_on_findings: bool = False,
) -> list[QueryFinding]:
    """Validate a diagnostic record for SPEC-008A §10.1, §10.2, §10.4 invariants.

    Checks: envelope completeness, runtime status truthfulness, concurrency
    conflict masking, freshness watermark validity, nonfuture watermarks,
    and optional latency/count non-negativity.

    Note: SemanticDiagnosticRecord schema enforces required envelope fields and
    status consistency at construction.  This function provides structured
    findings for both SemanticDiagnosticRecord and PartialDiagnosticExport,
    enabling diagnostic tooling to classify and surface violations explicitly.

    Args:
        record: A SemanticDiagnosticRecord or PartialDiagnosticExport instance.
        reference_watermark: ISO 8601 timestamp used as the freshness reference.
            If None, the current UTC time is used.
        max_age_hours: Maximum age of the watermark in hours before it is stale.
        raise_on_findings: If True, raise SemanticQueryValidationError on findings.

    Returns:
        List of QueryFinding instances.  Empty when all invariants pass.
    """
    findings: list[QueryFinding] = []
    raw = record.model_dump()
    provided_fields = record.model_fields_set

    # §10.4: required envelope field completeness
    for field in sorted(_REQUIRED_DIAGNOSTIC_FIELDS):
        val = raw.get(field)
        missing_from_partial = (
            isinstance(record, PartialDiagnosticExport)
            and field not in provided_fields
        )
        invalid_empty = val == "" or (
            field not in {"error_category", "evidence_ids"}
            and val is None
        )
        if missing_from_partial or invalid_empty:
            findings.append(QueryFinding(
                "DIAGNOSTIC_FIELD_MISSING",
                f"Incomplete diagnostic envelope: required field '{field}' "
                "is absent or empty (SPEC-008A §10.4).",
            ))
    if raw.get("schema_mode") == "schema2_bounded":
        for field in ("domain_contract_hash", "query_authority_hash"):
            if not raw.get(field):
                findings.append(QueryFinding(
                    "DIAGNOSTIC_FIELD_MISSING",
                    "Incomplete schema-2 diagnostic envelope: required field "
                    f"'{field}' is absent or empty.",
                ))

    result_cat = raw.get("result_category")
    final_status = raw.get("final_semantic_status")

    # §10.1: required-source failure must not be masked as success
    if (
        result_cat in _SOURCE_FAILURE_CATEGORIES
        and final_status == "success"
    ):
        findings.append(QueryFinding(
            "DIAGNOSTIC_STATUS_MASKED",
            f"final_semantic_status='success' with result_category="
            f"'{result_cat}': required-source failure is masked as semantic "
            "success. Failed required execution cannot produce a completed "
            "semantic success (SPEC-008A §10.1).",
        ))

    # §10.2: concurrency conflict must not be masked as success
    if result_cat == "concurrency_conflict" and final_status == "success":
        findings.append(QueryFinding(
            "DIAGNOSTIC_CONFLICT_MASKED",
            "result_category='concurrency_conflict' cannot yield "
            "final_semantic_status='success'. Concurrency conflicts must "
            "surface as concurrency_conflict or partial_result to preserve "
            "the first actionable failure (SPEC-008A §10.2).",
        ))
    if final_status == "success" and result_cat != "success":
        findings.append(QueryFinding(
            "DIAGNOSTIC_STATUS_MASKED",
            "final_semantic_status='success' requires result_category='success'.",
        ))
    if final_status == "success" and raw.get("static_validation_passed") is not True:
        findings.append(QueryFinding(
            "DIAGNOSTIC_VALIDATION_MISSING",
            "Semantic success requires static_validation_passed=True.",
        ))
    if final_status == "success" and (raw.get("query_row_count") or 0) <= 0:
        findings.append(QueryFinding(
            "DIAGNOSTIC_SUCCESS_WITHOUT_ROWS",
            "Semantic success requires a positive query_row_count.",
        ))
    if (
        raw.get("partial_snapshot") or raw.get("overlapping_snapshot")
    ) and final_status == "success":
        findings.append(QueryFinding(
            "DIAGNOSTIC_PARTIAL_SUCCESS",
            "Partial or overlapping snapshots cannot be reported as semantic success.",
        ))

    # §10.4: freshness watermark validation
    watermark = raw.get("export_freshness_watermark") or ""
    if not watermark:
        findings.append(QueryFinding(
            "DIAGNOSTIC_WATERMARK_MISSING",
            "SemanticDiagnosticRecord has no export_freshness_watermark. "
            "Without a freshness watermark the diagnostic cannot be correlated "
            "to a deployment observation window.",
        ))
    else:
        try:
            recorded_dt = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
            if recorded_dt.tzinfo is None:
                recorded_dt = recorded_dt.replace(tzinfo=timezone.utc)
            ref_str = reference_watermark or datetime.now(timezone.utc).isoformat()
            reference_dt = datetime.fromisoformat(ref_str.replace("Z", "+00:00"))
            # Always ensure reference_dt is UTC-aware to prevent aware-vs-naive TypeError
            if reference_dt.tzinfo is None:
                reference_dt = reference_dt.replace(tzinfo=timezone.utc)
            age_hours = (reference_dt - recorded_dt).total_seconds() / 3600.0
            if age_hours > max_age_hours:
                findings.append(QueryFinding(
                    "DIAGNOSTIC_WATERMARK_STALE",
                    f"Diagnostic envelope is stale: watermark is {age_hours:.1f}h "
                    f"before reference (max allowed: {max_age_hours}h). "
                    "Re-export diagnostics from the current deployment window.",
                ))
            # Nonfuture check: watermark must not exceed the observation time
            if recorded_dt > reference_dt:
                findings.append(QueryFinding(
                    "DIAGNOSTIC_WATERMARK_FUTURE",
                    "export_freshness_watermark is in the future relative to the "
                    "reference observation time. Watermarks must not exceed the "
                    "current observation time.",
                ))
        except ValueError as exc:
            findings.append(QueryFinding(
                "DIAGNOSTIC_WATERMARK_INVALID",
                f"export_freshness_watermark is not valid ISO 8601: {exc}",
            ))

    # §10.3/§10.4: successful records must carry non-empty evidence IDs.
    # Checked here for PartialDiagnosticExport (SemanticDiagnosticRecord enforces
    # this at schema-construction time via _validate_status_consistency).
    if final_status == "success" and not raw.get("evidence_ids"):
        findings.append(QueryFinding(
            "DIAGNOSTIC_EVIDENCE_MISSING",
            "final_semantic_status='success' requires non-empty evidence_ids. "
            "Successful runs must retain evidence IDs per SPEC-008A §10.3/§10.4.",
        ))

    # Optional field non-negativity (belt-and-suspenders for PartialDiagnosticExport)
    latency = raw.get("latency_ms")
    if latency is not None and latency < 0:
        findings.append(QueryFinding(
            "DIAGNOSTIC_NEGATIVE_LATENCY",
            f"latency_ms must be nonnegative, got {latency}.",
        ))
    row_count = raw.get("query_row_count")
    if row_count is not None and row_count < 0:
        findings.append(QueryFinding(
            "DIAGNOSTIC_NEGATIVE_ROW_COUNT",
            f"query_row_count must be nonnegative, got {row_count}.",
        ))

    if raise_on_findings and findings:
        raise SemanticQueryValidationError(findings)
    return findings
