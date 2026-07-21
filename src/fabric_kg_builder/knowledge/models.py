"""knowledge.models — capability result models and API-version selection.

AGK-002: Discovers whether the Azure AI Search service supports agentic
retrieval at GA (2026-04-01) or preview (2026-05-01-preview) and records
which features are available.  The selected API version is pinned for the
lifetime of the caller so every subsequent operation in the session uses the
same version.

GA features (2026-04-01):
    Knowledge sources (searchIndex kind), knowledge bases, direct retrieval,
    citation normalization.

Preview features (2026-05-01-preview, additive):
    fabricDataAgent source kind, fabricOntology source kind.
    Requires explicit ``prefer_preview=True`` AND ``preview_acknowledged=True``.

Capability discovery
--------------------
Calls ``GET {endpoint}/servicestatistics?api-version=<candidate>`` for each
candidate version in order.  **GA is always probed first.**  Preview is only
probed when the caller explicitly opts in with both ``prefer_preview=True``
and ``preview_acknowledged=True``.  A 2xx response indicates that version is
supported; a 4xx disqualifies it.  A connection failure propagates as-is so
callers know the service is unreachable.

Preview compliance
------------------
Preview features are subject to the Microsoft Preview Terms and are not
recommended for production workloads.  To use them the caller must pass
``prefer_preview=True`` together with ``preview_acknowledged=True`` to
:func:`discover_capabilities`.  This double-gate mirrors the repository-wide
preview/compliance acknowledgement pattern.

Disabled behaviour
------------------
If a requested feature requires a version the service does not support, the
operation raises :class:`FeatureNotAvailable` rather than silently falling
back to a noop.

Authentication
--------------
Search REST API accepts either an ``api-key`` header **or** an
``Authorization: Bearer <token>`` header — never both.  Use
:class:`SearchAuth` to carry the chosen credential.  For retrieval from
Fabric preview sources an additional per-request ``x-ms-query-source-authorization``
header must carry the end-user OBO token; this is separate from Search auth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .transport import HttpError, HttpRequest, HttpTransport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GA_VERSION = "2026-04-01"
_PREVIEW_VERSION = "2026-05-01-preview"

# Search token audience
_SEARCH_TOKEN_SCOPE = "https://search.azure.com/.default"

#: Human-readable notice required before enabling preview capabilities.
PREVIEW_COMPLIANCE_NOTICE = (
    "Azure AI Search agentic retrieval preview (2026-05-01-preview) features "
    "are subject to the Microsoft Preview Terms "
    "(https://azure.microsoft.com/en-us/support/legal/preview-supplemental-terms/) "
    "and are not recommended for production workloads. "
    "Explicitly pass prefer_preview=True and preview_acknowledged=True to opt in."
)


# ---------------------------------------------------------------------------
# Feature enum
# ---------------------------------------------------------------------------


class AgentFeature(Enum):
    """Azure AI Search agentic-retrieval features gated by API version."""

    # GA 2026-04-01
    KNOWLEDGE_SOURCES = "knowledgeSources"
    KNOWLEDGE_BASES = "knowledgeBases"
    RETRIEVE = "retrieve"

    # Preview 2026-05-01-preview (additive, requires explicit opt-in)
    FABRIC_DATA_AGENT_SOURCE = "fabricDataAgentSource"
    FABRIC_ONTOLOGY_SOURCE = "fabricOntologySource"


# Features available at each API version (additive set)
_GA_FEATURES: frozenset[AgentFeature] = frozenset(
    {
        AgentFeature.KNOWLEDGE_SOURCES,
        AgentFeature.KNOWLEDGE_BASES,
        AgentFeature.RETRIEVE,
    }
)

_PREVIEW_FEATURES: frozenset[AgentFeature] = _GA_FEATURES | frozenset(
    {
        AgentFeature.FABRIC_DATA_AGENT_SOURCE,
        AgentFeature.FABRIC_ONTOLOGY_SOURCE,
    }
)

_VERSION_FEATURES: dict[str, frozenset[AgentFeature]] = {
    _GA_VERSION: _GA_FEATURES,
    _PREVIEW_VERSION: _PREVIEW_FEATURES,
}

# GA is always the default probe order.  Preview is appended only when the
# caller has explicitly opted in (prefer_preview=True, preview_acknowledged=True).
_GA_PROBE_ORDER: list[str] = [_GA_VERSION]
_PREVIEW_PROBE_ORDER: list[str] = [_PREVIEW_VERSION, _GA_VERSION]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FeatureNotAvailable(Exception):
    """Raised when a requested feature requires a higher API version than the service supports.

    Attributes
    ----------
    feature : AgentFeature
        The feature that is not available.
    required_version : str
        The minimum API version that provides the feature.
    available_version : str | None
        The highest version the service actually supports, or ``None`` if the
        service supports no agentic-retrieval version at all.
    """

    def __init__(
        self,
        feature: AgentFeature,
        required_version: str,
        available_version: str | None,
    ) -> None:
        self.feature = feature
        self.required_version = required_version
        self.available_version = available_version
        super().__init__(
            f"Feature {feature.value!r} requires API version {required_version!r} "
            f"but service only supports {available_version!r}."
        )


class PreviewNotAcknowledged(Exception):
    """Raised when preview features are requested without the compliance acknowledgement.

    Pass both ``prefer_preview=True`` and ``preview_acknowledged=True`` to
    :func:`discover_capabilities` to opt in.  See :data:`PREVIEW_COMPLIANCE_NOTICE`.
    """

    def __init__(self) -> None:
        super().__init__(
            "Preview capabilities require explicit opt-in. "
            "Pass prefer_preview=True AND preview_acknowledged=True. "
            f"{PREVIEW_COMPLIANCE_NOTICE}"
        )


# ---------------------------------------------------------------------------
# Capability result
# ---------------------------------------------------------------------------


@dataclass
class CapabilityResult:
    """The capabilities discovered for a particular Search service endpoint.

    Attributes
    ----------
    endpoint : str
        The Search service endpoint (e.g. ``https://svc.search.windows.net``).
    api_version : str | None
        The API version selected by :func:`discover_capabilities`, or ``None``
        if no supported version was found.
    available_features : frozenset[AgentFeature]
        The set of features the service provides at *api_version*.
    is_preview : bool
        ``True`` when *api_version* is the preview version.  Always ``False``
        unless the caller explicitly opted in.
    """

    endpoint: str
    api_version: str | None
    available_features: frozenset[AgentFeature] = field(
        default_factory=frozenset
    )
    is_preview: bool = False

    def supports(self, feature: AgentFeature) -> bool:
        """Return ``True`` if *feature* is available at the discovered version."""
        return feature in self.available_features

    def require(self, feature: AgentFeature) -> None:
        """Raise :class:`FeatureNotAvailable` unless *feature* is available.

        Callers invoke this before any mutating operation to guarantee that
        disabled features never touch the service state.
        """
        if feature not in self.available_features:
            required = _minimum_version_for(feature)
            raise FeatureNotAvailable(
                feature=feature,
                required_version=required or _PREVIEW_VERSION,
                available_version=self.api_version,
            )


def _minimum_version_for(feature: AgentFeature) -> str | None:
    """Return the lowest API version that provides *feature*, or ``None``."""
    for version in [_GA_VERSION, _PREVIEW_VERSION]:
        if feature in _VERSION_FEATURES.get(version, frozenset()):
            return version
    return None


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------


@dataclass
class SearchAuth:
    """Search service authentication credential.

    Exactly one of *api_key* or *token* must be provided (not both).
    The optional *obo_token* is the end-user OBO token required for Fabric
    preview sources; it is sent as ``x-ms-query-source-authorization`` and
    must **never** be used as the primary Search auth.

    Attributes
    ----------
    api_key : str | None
        API key for ``api-key:`` header auth.  Mutually exclusive with *token*.
    token : str | None
        Bearer token for ``Authorization: Bearer`` auth.  Mutually exclusive
        with *api_key*.
    obo_token : str | None
        OBO token for Fabric preview sources.  Sent as
        ``x-ms-query-source-authorization: Bearer <obo_token>``.
        Never logged, never substituted for Search auth.
    """

    api_key: str | None = None
    token: str | None = None
    obo_token: str | None = None

    def __post_init__(self) -> None:
        if self.api_key and self.token:
            raise ValueError(
                "SearchAuth: provide either api_key OR token, not both."
            )
        if not self.api_key and not self.token:
            raise ValueError(
                "SearchAuth: one of api_key or token must be provided."
            )

    def to_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Build request headers for Search REST calls.

        Credentials are included but never echoed to logs.  The OBO token is
        added only when present.

        Parameters
        ----------
        extra : dict[str, str] | None
            Additional headers to merge in (lower priority than auth headers).

        Returns
        -------
        dict[str, str]
            Headers dict ready to pass to :class:`HttpRequest`.
        """
        hdrs: dict[str, str] = {"Content-Type": "application/json"}
        if extra:
            hdrs.update(extra)
        if self.api_key:
            hdrs["api-key"] = self.api_key
        else:
            hdrs["Authorization"] = f"Bearer {self.token}"
        if self.obo_token:
            hdrs["x-ms-query-source-authorization"] = f"Bearer {self.obo_token}"
        return hdrs


# ---------------------------------------------------------------------------
# Capability discovery
# ---------------------------------------------------------------------------


def _default_token_provider() -> str:
    """Obtain a bearer token for the Search service via DefaultAzureCredential."""
    from azure.identity import DefaultAzureCredential  # noqa: PLC0415

    cred = DefaultAzureCredential()
    return cred.get_token(_SEARCH_TOKEN_SCOPE).token

def discover_capabilities(
    endpoint: str,
    transport: HttpTransport,
    token_provider: Callable[[], str] | None = None,
    *,
    prefer_preview: bool = False,
    preview_acknowledged: bool = False,
) -> CapabilityResult:
    """Probe *endpoint* and return a :class:`CapabilityResult`.

    **GA (2026-04-01) is always the default.**  The preview version
    (2026-05-01-preview) is only probed when *prefer_preview=True* AND
    *preview_acknowledged=True*.  Passing ``prefer_preview=True`` alone raises
    :class:`PreviewNotAcknowledged` -- this mirrors the repository-wide
    preview/compliance acknowledgement pattern.

    Parameters
    ----------
    endpoint:
        Search service root URL (e.g. ``https://svc.search.windows.net``).
        Trailing slashes are stripped.
    transport:
        Injectable :class:`HttpTransport` (inject ``FakeTransport`` in tests).
    token_provider:
        Callable that returns a bearer token.  Defaults to
        ``DefaultAzureCredential`` scoped to ``https://search.azure.com/.default``.
    prefer_preview:
        When ``True`` (together with *preview_acknowledged=True*), probe the
        preview version before GA.  **Do not set this automatically** based on
        source kinds -- only set when the caller explicitly requests preview.
    preview_acknowledged:
        Must be ``True`` when *prefer_preview=True*.  Acts as the compliance
        acknowledgement for preview usage.

    Returns
    -------
    CapabilityResult
        Pinned to the discovered version; ``api_version=None`` if no
        candidate version is accepted.

    Raises
    ------
    PreviewNotAcknowledged
        When ``prefer_preview=True`` but ``preview_acknowledged=False``.
    HttpError
        On connection errors (status_code=0) -- these are not silently swallowed.
    """
    if prefer_preview and not preview_acknowledged:
        raise PreviewNotAcknowledged()

    ep = endpoint.rstrip("/")
    tp = token_provider or _default_token_provider
    token = tp()

    probe_order = _PREVIEW_PROBE_ORDER if (prefer_preview and preview_acknowledged) else _GA_PROBE_ORDER

    for version in probe_order:
        url = f"{ep}/servicestatistics?api-version={version}"
        req = HttpRequest(
            method="GET",
            url=url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        logger.debug("[capability] probing %s", url)
        try:
            resp = transport.send(req)
        except HttpError as exc:
            if exc.status_code == 0:
                raise  # connection failure -- propagate
            # 4xx: this version is not supported
            logger.debug(
                "[capability] version %s not accepted (HTTP %s)", version, exc.status_code
            )
            continue

        if resp.status_code < 300:
            features = _VERSION_FEATURES[version]
            is_prev = version == _PREVIEW_VERSION
            logger.info(
                "[capability] endpoint=%s api_version=%s preview=%s features=%s",
                ep,
                version,
                is_prev,
                [f.value for f in features],
            )
            return CapabilityResult(
                endpoint=ep,
                api_version=version,
                available_features=features,
                is_preview=is_prev,
            )
        # 3xx or unexpected -- treat as not supported
        logger.debug(
            "[capability] version %s unexpected status %s", version, resp.status_code
        )

    logger.warning("[capability] endpoint=%s: no agentic-retrieval version supported", ep)
    return CapabilityResult(
        endpoint=ep,
        api_version=None,
        available_features=frozenset(),
        is_preview=False,
    )


# ---------------------------------------------------------------------------
# Legacy helpers retained for backward compatibility
# ---------------------------------------------------------------------------


def pinned_headers(
    token: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build standard Bearer-token headers for a Search REST call.

    Prefer :class:`SearchAuth` for new code.  This helper is kept for
    backward compatibility with code that supplies a token directly.

    The token value is **never** logged by this function.
    """
    hdrs: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if extra:
        hdrs.update(extra)
    return hdrs