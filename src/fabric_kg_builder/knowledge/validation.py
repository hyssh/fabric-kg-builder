"""knowledge.validation — source validation and five-source cap enforcement.

AGK-006: Validates a list of knowledge/data sources before they are sent to
either the Search knowledge base or the Fabric Data Agent.  Raises structured
errors so callers surface clear, actionable messages rather than opaque HTTP 400s.

Rules enforced
--------------
1. **Source count ≤ 5** (both Search KB and Fabric Data Agent).
2. **Source names must be unique** within the set.
3. **Source types must be non-empty strings**.
4. **Preview-only types require preview API version** — attempting to use
   ``FabricDataAgent`` or ``FabricOntology`` against a GA-only endpoint raises
   :class:`SourceTypeUnavailable`.
5. **Source policy** — required types must be present, prohibited types must be
   absent.  Use :func:`validate_source_policy` and
   :func:`validate_published_source_policy`.
6. **Text limits** — global instructions, per-source instructions, source
   descriptions, and few-shot payload have named character-count caps.  Use
   :func:`validate_data_agent_text`.  Duplicate-instruction detection uses
   :func:`validate_instruction_deduplication`.

Usage::

    from fabric_kg_builder.knowledge.validation import validate_sources, SourceSpec

    sources = [
        SourceSpec(name="search-idx", source_type="AzureAISearch"),
        SourceSpec(name="my-da", source_type="FabricDataAgent"),
    ]
    validate_sources(sources, api_version="2026-05-01-preview")  # ok
    validate_sources(sources, api_version="2026-04-01")          # raises SourceTypeUnavailable
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .data_agent import DataAgentSpec, DataAgentStageSnapshot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Text limit constants (issue #10 — named constants, no scattered literals)
# ---------------------------------------------------------------------------

MAX_GLOBAL_INSTRUCTION_CHARS: int = 4_000
MAX_SOURCE_INSTRUCTION_CHARS: int = 2_000
MAX_SOURCE_DESCRIPTION_CHARS: int = 500
MAX_FEW_SHOT_COUNT: int = 5
MAX_FEW_SHOT_PAYLOAD_CHARS: int = 10_000

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SOURCES = 5

_GA_VERSION = "2026-04-01"
_PREVIEW_VERSION = "2026-05-01-preview"

# Kind values available at each API version (matching the official REST ``kind`` field)
_GA_TYPES: frozenset[str] = frozenset({"searchIndex"})
_PREVIEW_TYPES: frozenset[str] = _GA_TYPES | frozenset(
    {"fabricDataAgent", "fabricOntology"}
)

_VERSION_TYPES: dict[str, frozenset[str]] = {
    _GA_VERSION: _GA_TYPES,
    _PREVIEW_VERSION: _PREVIEW_TYPES,
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    """A single knowledge / data source descriptor.

    Attributes
    ----------
    name : str
        Unique name for this source (must be non-empty).
    source_type : str
        Source kind string per the official API: ``"searchIndex"``,
        ``"fabricDataAgent"``, or ``"fabricOntology"``.
    """

    name: str
    source_type: str


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class ValidationError(Exception):
    """Base class for all source-validation errors."""


class SourceCapError(ValidationError):
    """Raised when the number of sources exceeds :data:`MAX_SOURCES`.

    Attributes
    ----------
    count : int
        The number of sources provided.
    cap : int
        The maximum allowed (always :data:`MAX_SOURCES`).
    """

    def __init__(self, count: int, cap: int = MAX_SOURCES) -> None:
        self.count = count
        self.cap = cap
        super().__init__(
            f"Source count {count} exceeds the maximum of {cap}. "
            "Remove sources to comply with the five-source cap."
        )


class DuplicateSourceNameError(ValidationError):
    """Raised when two or more sources share the same name.

    Attributes
    ----------
    duplicates : list[str]
        The duplicated names.
    """

    def __init__(self, duplicates: list[str]) -> None:
        self.duplicates = duplicates
        super().__init__(
            f"Duplicate source names: {duplicates!r}. Each source must have a unique name."
        )


class InvalidSourceError(ValidationError):
    """Raised when a source has an empty name or type.

    Attributes
    ----------
    index : int
        0-based position of the invalid source in the list.
    detail : str
        Description of what is invalid.
    """

    def __init__(self, index: int, detail: str) -> None:
        self.index = index
        self.detail = detail
        super().__init__(f"Source at index {index} is invalid: {detail}")


class SourceTypeUnavailable(ValidationError):
    """Raised when a source type requires a newer API version than is available.

    Attributes
    ----------
    source_name : str
        Name of the source whose type is unavailable.
    source_type : str
        The type that is unavailable at *api_version*.
    api_version : str
        The API version against which validation was run.
    required_version : str
        The minimum version that supports *source_type*.
    """

    def __init__(
        self,
        source_name: str,
        source_type: str,
        api_version: str,
        required_version: str,
    ) -> None:
        self.source_name = source_name
        self.source_type = source_type
        self.api_version = api_version
        self.required_version = required_version
        super().__init__(
            f"Source {source_name!r} uses type {source_type!r} which is not available at "
            f"api-version {api_version!r}. Minimum required version: {required_version!r}."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_sources(
    sources: list[SourceSpec],
    api_version: str = _GA_VERSION,
) -> None:
    """Validate *sources* against the given *api_version*.

    Raises the first violation encountered; does **not** collect all errors.
    Order of checks: count cap → blank fields → duplicates → type availability.

    Parameters
    ----------
    sources:
        The list of :class:`SourceSpec` objects to validate.
    api_version:
        The Search / Fabric API version string.  Defaults to GA.

    Raises
    ------
    SourceCapError
        More than :data:`MAX_SOURCES` sources.
    InvalidSourceError
        A source has an empty ``name`` or ``source_type``.
    DuplicateSourceNameError
        Two or more sources share the same name.
    SourceTypeUnavailable
        A source type is not available at *api_version*.
    """
    # 1. Cap
    if len(sources) > MAX_SOURCES:
        raise SourceCapError(len(sources))

    # 2. Blank fields
    for i, src in enumerate(sources):
        if not src.name or not src.name.strip():
            raise InvalidSourceError(i, "name must be a non-empty string")
        if not src.source_type or not src.source_type.strip():
            raise InvalidSourceError(i, "source_type must be a non-empty string")

    # 3. Duplicate names
    names = [src.name for src in sources]
    seen: set[str] = set()
    dups: list[str] = []
    for name in names:
        if name in seen:
            dups.append(name)
        seen.add(name)
    if dups:
        raise DuplicateSourceNameError(dups)

    # 4. Type availability
    allowed = _VERSION_TYPES.get(api_version, _GA_TYPES)
    for src in sources:
        if src.source_type not in allowed:
            # Find the minimum version that supports this type
            required = _min_version_for_type(src.source_type)
            raise SourceTypeUnavailable(
                source_name=src.name,
                source_type=src.source_type,
                api_version=api_version,
                required_version=required or _PREVIEW_VERSION,
            )

    logger.debug(
        "[validation] %d source(s) validated against api-version=%s",
        len(sources),
        api_version,
    )


def _min_version_for_type(source_type: str) -> str | None:
    """Return the lowest API version that supports *source_type*, or ``None``."""
    for ver in [_GA_VERSION, _PREVIEW_VERSION]:
        if source_type in _VERSION_TYPES.get(ver, frozenset()):
            return ver
    return None


# ---------------------------------------------------------------------------
# Source policy model (issue #9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourcePolicy:
    """Declares which Data Agent source types are required or prohibited.

    The policy is **closed-world**: any source type that is not in *required*
    and not in *allowed_extra* is rejected, even if it is not listed in
    *prohibited*.  Use *allowed_extra* to permit additional types beyond the
    required set without making them mandatory.

    Attributes
    ----------
    required : frozenset[str]
        Source types that MUST appear in the spec (e.g. ``{"ontology", "graph"}``).
    prohibited : frozenset[str]
        Source types that MUST NOT appear in the spec (e.g. ``{"lakehouse"}``).
        Redundant with closed-world enforcement but kept for explicit documentation
        and published read-back validation where the full allowed set is not always
        derivable from ``required`` alone.
    allowed_extra : frozenset[str]
        Source types that are permitted in addition to *required* types but are
        not mandatory.  Closed-world enforcement passes these through silently.
    """

    required: frozenset[str]
    prohibited: frozenset[str] = field(default_factory=frozenset)
    allowed_extra: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        overlap = self.required & self.prohibited
        if overlap:
            raise ValueError(
                f"SourcePolicy has types in both required and prohibited: {sorted(overlap)}"
            )
        overlap_extra = self.allowed_extra & self.prohibited
        if overlap_extra:
            raise ValueError(
                f"SourcePolicy has types in both allowed_extra and prohibited: {sorted(overlap_extra)}"
            )


class SourcePolicyViolation(ValidationError):
    """Raised when a Data Agent spec violates the source selection policy.

    Attributes
    ----------
    code : str
        Machine-readable error code.
    field : str
        The policy aspect that was violated.
    """

    def __init__(self, code: str, field_name: str, message: str) -> None:
        self.code = code
        self.field = field_name
        super().__init__(f"{code} [{field_name}]: {message}")


def validate_source_policy(
    spec: DataAgentSpec,
    policy: SourcePolicy,
) -> None:
    """Validate that *spec* satisfies *policy*.

    Checks that every required type is present and no prohibited type is
    present.  Raises on the first violation.

    Parameters
    ----------
    spec:
        The assembled :class:`~fabric_kg_builder.knowledge.data_agent.DataAgentSpec`.
    policy:
        The :class:`SourcePolicy` to enforce.

    Raises
    ------
    SourcePolicyViolation
        A required type is missing or a prohibited type is present.
    """
    actual_types = frozenset(src.source_type for src in spec.sources)  # type: ignore[union-attr]
    for required_type in sorted(policy.required):
        if required_type not in actual_types:
            raise SourcePolicyViolation(
                "SOURCE_POLICY_MISSING_REQUIRED",
                required_type,
                f"Required source type {required_type!r} is not in the spec. "
                f"Configured sources: {sorted(actual_types)}.",
            )
    for prohibited_type in sorted(policy.prohibited):
        if prohibited_type in actual_types:
            raise SourcePolicyViolation(
                "SOURCE_POLICY_PROHIBITED_PRESENT",
                prohibited_type,
                f"Prohibited source type {prohibited_type!r} is present in the spec. "
                f"Remove it before deploying.",
            )
    # Closed-world: reject types not in required or allowed_extra
    allowed = policy.required | policy.allowed_extra
    for extra_type in sorted(actual_types - allowed):
        raise SourcePolicyViolation(
            "SOURCE_POLICY_EXTRA_TYPE",
            extra_type,
            f"Source type {extra_type!r} is not in the configured source set "
            f"{sorted(allowed)}. "
            "Add it to required or allowed_extra, or remove it from the spec.",
        )


def validate_published_source_policy(
    snapshot: DataAgentStageSnapshot,
    policy: SourcePolicy,
) -> None:
    """Validate that the published/read-back snapshot satisfies *policy*.

    This catches Fabric-side normalization that adds or removes source types
    compared to the configured set.

    Parameters
    ----------
    snapshot:
        The :class:`~fabric_kg_builder.knowledge.data_agent.DataAgentStageSnapshot`
        decoded from the published definition.
    policy:
        The :class:`SourcePolicy` to enforce.

    Raises
    ------
    SourcePolicyViolation
        The published source types deviate from the configured policy.
    """
    published_types = frozenset(
        str(src.get("type") or "") for src in snapshot.sources  # type: ignore[union-attr]
    )
    for required_type in sorted(policy.required):
        if required_type not in published_types:
            raise SourcePolicyViolation(
                "PUBLISHED_SOURCE_POLICY_MISSING_REQUIRED",
                required_type,
                f"Required source type {required_type!r} is absent from the "
                f"published definition. Published types: {sorted(published_types)}.",
            )
    for prohibited_type in sorted(policy.prohibited):
        if prohibited_type in published_types:
            raise SourcePolicyViolation(
                "PUBLISHED_SOURCE_POLICY_PROHIBITED_PRESENT",
                prohibited_type,
                f"Prohibited source type {prohibited_type!r} is present in the "
                f"published definition. Published types: {sorted(published_types)}.",
            )
    # Closed-world: reject published types not in required or allowed_extra
    allowed = policy.required | policy.allowed_extra
    for extra_type in sorted(published_types - allowed):
        raise SourcePolicyViolation(
            "PUBLISHED_SOURCE_POLICY_EXTRA_TYPE",
            extra_type,
            f"Published source type {extra_type!r} is not in the configured set "
            f"{sorted(allowed)}. "
            "Fabric may have added an unexpected source; review the deployment.",
        )


# ---------------------------------------------------------------------------
# Text validation model (issue #10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextValidationResult:
    """Result for one text-limit check.

    Attributes
    ----------
    field : str
        Field identifier, e.g. ``"global.instruction"`` or
        ``"graph.dataSourceInstructions"``.
    actual : int
        Measured character count.
    limit : int
        Configured maximum.
    passed : bool
        ``True`` when ``actual <= limit``.
    remediation : str
        Human-readable suggestion when ``passed`` is ``False``.
    """

    field: str
    actual: int
    limit: int
    passed: bool
    remediation: str = ""


class TextLimitViolation(ValidationError):
    """Raised when a text field exceeds its configured limit.

    Attributes
    ----------
    code : str
        Machine-readable error code: ``"DATA_AGENT_TEXT_LIMIT"``.
    field : str
        The field that exceeds the limit.
    actual : int
        Actual character count.
    limit : int
        Configured maximum.
    remediation : str
        Suggested remediation.
    """

    def __init__(
        self,
        field: str,
        actual: int,
        limit: int,
        remediation: str = "",
    ) -> None:
        self.code = "DATA_AGENT_TEXT_LIMIT"
        self.field = field
        self.actual = actual
        self.limit = limit
        self.remediation = remediation
        super().__init__(
            f"ERROR DATA_AGENT_TEXT_LIMIT:\n"
            f'Field "{field}" contains {actual:,} characters; '
            f"the configured maximum is {limit:,}.\n"
            + (remediation if remediation else "")
        )


def validate_data_agent_text(
    spec: DataAgentSpec,
) -> list[TextValidationResult]:
    """Validate all text fields in *spec* against named limits.

    Returns one :class:`TextValidationResult` per checked field.  Does **not**
    raise; callers can decide whether to fail on the first failure.

    Parameters
    ----------
    spec:
        The assembled :class:`~fabric_kg_builder.knowledge.data_agent.DataAgentSpec`.

    Returns
    -------
    list[TextValidationResult]
        One entry per field, in declaration order:
        global instruction → per-source instruction → per-source description
        → per-source few-shot count → few-shot payload size.
    """
    results: list[TextValidationResult] = []

    global_text = str(spec.instruction or "")  # type: ignore[union-attr]
    results.append(
        TextValidationResult(
            field="global.instruction",
            actual=len(global_text),
            limit=MAX_GLOBAL_INSTRUCTION_CHARS,
            passed=len(global_text) <= MAX_GLOBAL_INSTRUCTION_CHARS,
            remediation=(
                "Shorten the global instruction. Move per-source details into "
                "each source's dataSourceInstructions."
            ),
        )
    )

    for src in spec.sources:  # type: ignore[union-attr]
        src_type = str(src.source_type or "")
        instr_text = str(src.instructions or "")
        results.append(
            TextValidationResult(
                field=f"{src_type}.dataSourceInstructions",
                actual=len(instr_text),
                limit=MAX_SOURCE_INSTRUCTION_CHARS,
                passed=len(instr_text) <= MAX_SOURCE_INSTRUCTION_CHARS,
                remediation=(
                    f"Move schema detail from {src_type!r} instructions into "
                    "selected elements or validated few-shots."
                ),
            )
        )
        desc_text = str(src.description or "")
        results.append(
            TextValidationResult(
                field=f"{src_type}.userDescription",
                actual=len(desc_text),
                limit=MAX_SOURCE_DESCRIPTION_CHARS,
                passed=len(desc_text) <= MAX_SOURCE_DESCRIPTION_CHARS,
                remediation=(
                    f"Shorten the {src_type!r} source description to a concise "
                    "statement of its purpose."
                ),
            )
        )
        few_shots = src.few_shots or []
        fs_count = len(few_shots)
        results.append(
            TextValidationResult(
                field=f"{src_type}.fewShots.count",
                actual=fs_count,
                limit=MAX_FEW_SHOT_COUNT,
                passed=fs_count <= MAX_FEW_SHOT_COUNT,
                remediation=(
                    f"Remove {fs_count - MAX_FEW_SHOT_COUNT} few-shot example(s) "
                    "from the Graph source."
                ),
            )
        )
        if few_shots:
            payload_text = json.dumps(
                [fs.to_dict() for fs in few_shots],  # type: ignore[union-attr]
                ensure_ascii=False,
            )
            payload_size = len(payload_text)
            results.append(
                TextValidationResult(
                    field=f"{src_type}.fewShots.payloadChars",
                    actual=payload_size,
                    limit=MAX_FEW_SHOT_PAYLOAD_CHARS,
                    passed=payload_size <= MAX_FEW_SHOT_PAYLOAD_CHARS,
                    remediation=(
                        "Reduce the total few-shot payload size by shortening queries "
                        "or reducing the number of examples."
                    ),
                )
            )

    return results


def _normalize_for_dedup(text: str) -> str:
    """Normalize *text* for duplicate detection.

    Strips leading/trailing whitespace per line, collapses all internal
    whitespace runs to a single space, collapses multiple blank lines to one,
    lower-cases, and Unicode-normalizes (NFC).  This allows whitespace and
    case differences to be treated as equal.
    """
    import re as _re
    lines = [
        unicodedata.normalize("NFC", _re.sub(r"\s+", " ", line).strip()).lower()
        for line in text.splitlines()
    ]
    normalized_lines: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run == 1:
                normalized_lines.append(line)
        else:
            blank_run = 0
            normalized_lines.append(line)
    return "\n".join(normalized_lines).strip()


def validate_instruction_deduplication(
    spec: DataAgentSpec,
) -> list[str]:
    """Detect duplicate or near-identical instruction blocks across scopes.

    Checks for exact duplication (after normalization) between:
    - global instruction and each per-source instruction.
    - pairs of per-source instructions.

    Near-identical means the normalized texts are equal (whitespace and
    case-insensitive comparison).  Intentionally short shared terminology
    (< 200 characters normalized) is exempt from the check.

    Parameters
    ----------
    spec:
        The assembled :class:`~fabric_kg_builder.knowledge.data_agent.DataAgentSpec`.

    Returns
    -------
    list[str]
        Human-readable descriptions of each detected duplication.  An empty
        list means no duplicates were detected.
    """
    _DEDUP_MIN_LENGTH = 200

    duplicates: list[str] = []
    global_text = str(spec.instruction or "")  # type: ignore[union-attr]
    global_norm = _normalize_for_dedup(global_text)

    sources = list(spec.sources)  # type: ignore[union-attr]
    source_norms: list[tuple[str, str]] = []
    for src in sources:
        src_type = str(src.source_type or "")
        instr = str(src.instructions or "")
        norm = _normalize_for_dedup(instr)
        source_norms.append((src_type, norm))

    for src_type, norm in source_norms:
        if (
            len(norm) >= _DEDUP_MIN_LENGTH
            and global_norm
            and norm == global_norm
        ):
            duplicates.append(
                f"global.instruction is identical to {src_type}.dataSourceInstructions "
                f"({len(norm)} chars normalized). "
                "Move per-source detail out of the global instruction."
            )

    for i, (type_a, norm_a) in enumerate(source_norms):
        for type_b, norm_b in source_norms[i + 1 :]:
            if (
                len(norm_a) >= _DEDUP_MIN_LENGTH
                and norm_a == norm_b
            ):
                duplicates.append(
                    f"{type_a}.dataSourceInstructions is identical to "
                    f"{type_b}.dataSourceInstructions ({len(norm_a)} chars normalized). "
                    "Each source should have scope-specific instructions."
                )

    return duplicates


# ---------------------------------------------------------------------------
# Graph few-shot contract gate (issue #10)
# ---------------------------------------------------------------------------


class FewShotContractViolation(ValidationError):
    """Raised when a compiled competency contract exists but no Graph few-shots survive.

    Attributes
    ----------
    code : str
        Machine-readable error code: ``"GRAPH_FEW_SHOTS_REQUIRED"``.
    """

    def __init__(self, message: str) -> None:
        self.code = "GRAPH_FEW_SHOTS_REQUIRED"
        super().__init__(f"GRAPH_FEW_SHOTS_REQUIRED: {message}")


def validate_graph_few_shots(
    spec: DataAgentSpec,
    *,
    contract_exists: bool,
) -> None:
    """Hard-fail when a compiled competency contract exists but no Graph few-shots survive.

    When *contract_exists* is ``False`` (no compiled competency contract is
    present) zero few-shots is acceptable for backward compatibility.

    Parameters
    ----------
    spec:
        The assembled :class:`~fabric_kg_builder.knowledge.data_agent.DataAgentSpec`.
    contract_exists:
        ``True`` when a compiled competency contract (``competency-contract.json``)
        was found on disk and loaded.  ``False`` when no such contract exists.

    Raises
    ------
    FewShotContractViolation
        *contract_exists* is ``True`` and no Graph source has at least one
        surviving few-shot example.
    """
    if not contract_exists:
        return
    for src in spec.sources:  # type: ignore[union-attr]
        if str(getattr(src, "source_type", None) or "") == "graph":
            count = len(src.few_shots or [])  # type: ignore[union-attr]
            if count == 0:
                raise FewShotContractViolation(
                    "A compiled competency contract exists but no Graph few-shot "
                    "examples survived validation. "
                    "Review competency-contract.json: ensure cases have "
                    "probes.direct_graph with static_validation_passed=true "
                    "and a non-empty query."
                )
            return
