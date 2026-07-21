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

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

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
