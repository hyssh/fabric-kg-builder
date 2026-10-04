"""Production community-partition provider for the v2 derived hierarchy.

This module is PRODUCTION-ONLY: it exposes exactly one real provider, a
lazy wrapper around ``graspologic_native==1.2.5``'s ``hierarchical_leiden``.
The package is never imported at module load time — only inside
``NativeLeidenProvider.partition`` — so that importing this module (and the
rest of the v2 derived-hierarchy code) never requires the native dependency
to be installed. If it is not installed, ``partition`` raises
``NativeProviderUnavailableError`` explicitly; there is no algorithmic
fallback, and no test-only fake is importable from here.

Before any import is even attempted, the running interpreter is checked
against ``graspologic-native==1.2.5``'s own published
``Requires-Python: >=3.8,<3.14`` range; outside that range raises
``NativeProviderUnsupportedInterpreterError`` instead of attempting (and
getting an opaque failure from, or — worse — silently skipping) the
import. This is distinct from "not installed": selecting the optional
``leiden`` dependency-group extra (see ``pyproject.toml``) on an
unsupported interpreter such as this project's own Python 3.14 default
does NOT make native Leiden available, and that must be reported
explicitly rather than conflated with a generic "package not found".

If the package IS importable, its installed distribution version is also
checked against the exact ``NATIVE_PACKAGE_VERSION`` pin (via
``importlib.metadata.version``) before any call is made; a mismatch raises
``NativeProviderVersionMismatchError`` rather than assuming an unverified
version exposes the same API surface. Supported-runtime contract: on a
``>=3.8,<3.14`` interpreter with ``graspologic-native==1.2.5`` actually
installed, the "import succeeds AND version matches" happy path calls
``gn.hierarchical_leiden(...)`` with the exact keyword signature below,
which has been confirmed against the real installed distribution (an
isolated-venv check outside this module's own offline unit-test suite).
That suite itself only has the dependency-free interpreter/version
branches available to exercise directly — "not installed", "unsupported
interpreter", and (via monkeypatched ``importlib.metadata.version``)
"version mismatch" — because the native package is not installed in this
child session's own test environment; it does not re-verify the pinned
version's import/call behavior itself, that verification having already
been performed separately against the real distribution.

Unit tests needing a dependency-free ``CommunityProvider`` to exercise the
hierarchy/validation logic end-to-end must import
``DeterministicConnectedComponentsProvider`` from the separate, clearly
test-only module ``community_provider_testing`` instead — it is
intentionally NOT re-exported from this module so it can never be mistaken
for, or wired in as, an operator-usable substitute for real Leiden
partitioning.
"""

from __future__ import annotations

import importlib.metadata
import sys

from fabric_kg_builder.graph.community_contracts import (
    NATIVE_PACKAGE_MAX_PYTHON_EXCLUSIVE,
    NATIVE_PACKAGE_MIN_PYTHON,
    NATIVE_PACKAGE_NAME,
    NATIVE_PACKAGE_VERSION,
    CommunityProvider,
    NativePartitionEntry,
    NativeProviderUnavailableError,
    NativeProviderUnsupportedInterpreterError,
)


def _check_interpreter_supported() -> None:
    """Raise ``NativeProviderUnsupportedInterpreterError`` if the running
    interpreter falls outside ``graspologic-native==1.2.5``'s own published
    ``Requires-Python`` range. Called before any import is attempted, so an
    unsupported interpreter never even reaches an ``ImportError`` branch
    (which would be a correct-but-misleading "not installed" signal)."""
    running = sys.version_info[:3]
    if running[:2] < NATIVE_PACKAGE_MIN_PYTHON or running[:2] >= NATIVE_PACKAGE_MAX_PYTHON_EXCLUSIVE:
        raise NativeProviderUnsupportedInterpreterError(running)


class NativeProviderVersionMismatchError(NativeProviderUnavailableError):
    """Raised when ``graspologic_native`` is importable but its installed
    distribution version does not match the exactly-pinned
    ``NATIVE_PACKAGE_VERSION``.

    This is a distinct failure from "not installed": the module imported,
    but we never assume an unverified version exposes the same API surface
    (parameter names/order/semantics) as the one pin this provider was
    written against. A subclass of ``NativeProviderUnavailableError`` so
    every existing caller that already treats "native unavailable" as an
    explicit, non-fallback failure automatically also treats a version
    mismatch the same way, with no silent fallback and no assumption of
    compatibility.
    """

    def __init__(self, installed_version: str, expected_version: str = NATIVE_PACKAGE_VERSION):
        self.installed_version = installed_version
        self.expected_version = expected_version
        # Deliberately skip NativeProviderUnavailableError.__init__ (its
        # "not installed" message would be misleading here) and build our
        # own explicit mismatch message via RuntimeError directly.
        RuntimeError.__init__(
            self,
            f"Installed '{NATIVE_PACKAGE_NAME}' distribution version "
            f"'{installed_version}' does not match the exactly-pinned "
            f"'{expected_version}' this provider was written against. "
            "No API-compatibility assumption is made across versions; "
            "install exactly the pinned version to proceed."
        )
        self.package_name = NATIVE_PACKAGE_NAME
        self.package_version = expected_version


class NativeLeidenProvider:
    """Lazy wrapper around ``graspologic_native.hierarchical_leiden``.

    Matches the call signature confirmed from GraphRAG's pinned
    ``graphs/hierarchical_leiden.py`` (commit
    769542fbf1d8e5b4c6a8677fefc34621c87894c5), the only public reference
    implementation available for this exact native API surface, and
    separately confirmed against the real installed
    ``graspologic-native==1.2.5`` distribution's own API in an isolated
    venv (outside this module's own test suite, which runs without the
    dependency installed).
    """

    provider_id = "leiden_native"
    provider_version = NATIVE_PACKAGE_VERSION

    def __init__(self) -> None:
        # No import attempt at construction time either — defer entirely to
        # first use so that constructing (e.g. for provider_id/version
        # introspection) never requires the dependency.
        self._module = None

    def _ensure_module(self):
        if self._module is not None:
            return self._module
        _check_interpreter_supported()
        try:
            import graspologic_native as gn  # type: ignore[import-not-found]
        except ImportError as exc:
            raise NativeProviderUnavailableError(
                NATIVE_PACKAGE_NAME, NATIVE_PACKAGE_VERSION
            ) from exc
        # Importable is not sufficient: verify the actually-installed
        # distribution version matches the exact pin this wrapper's call
        # signature/semantics were written against. Never assume API
        # compatibility across versions from a passing import alone.
        try:
            installed_version = importlib.metadata.version(NATIVE_PACKAGE_NAME)
        except importlib.metadata.PackageNotFoundError as exc:
            # Importable under a different distribution name/path than
            # metadata reports — treat as unavailable rather than guess.
            raise NativeProviderUnavailableError(
                NATIVE_PACKAGE_NAME, NATIVE_PACKAGE_VERSION
            ) from exc
        if installed_version != NATIVE_PACKAGE_VERSION:
            raise NativeProviderVersionMismatchError(installed_version, NATIVE_PACKAGE_VERSION)
        self._module = gn
        return gn

    def partition(
        self,
        edges: list[tuple[str, str, float]],
        *,
        max_cluster_size: int,
        seed: int,
    ) -> list[NativePartitionEntry]:
        gn = self._ensure_module()
        raw = gn.hierarchical_leiden(
            edges=edges,
            max_cluster_size=max_cluster_size,
            seed=seed,
            starting_communities=None,
            resolution=1.0,
            randomness=0.001,
            use_modularity=True,
            iterations=1,
        )
        return [
            NativePartitionEntry(
                node_id=entry.node,
                cluster_id=entry.cluster,
                parent_cluster_id=entry.parent_cluster,
                level=entry.level,
                is_final_cluster=entry.is_final_cluster,
            )
            for entry in raw
        ]


def is_native_available() -> bool:
    """Best-effort, side-effect-free check for whether the pinned native
    dependency is importable. Does not attempt to install or build it.

    Returns ``False`` (never raises) for an unsupported interpreter — no
    import is attempted in that case either, for the same reason
    ``_ensure_module`` skips it: an out-of-range interpreter cannot have a
    compatible wheel resolved for it, so there is nothing meaningful to
    import. Also returns ``False`` for an installed distribution whose
    version does not exactly match ``NATIVE_PACKAGE_VERSION`` (or whose
    distribution metadata cannot be found at all) — this mirrors
    ``_ensure_module``'s own version-pin check exactly, so this boolean
    never claims availability for a module that ``_ensure_module`` would
    actually reject with ``NativeProviderVersionMismatchError``. Callers
    needing the *distinction* between the specific unavailable reasons
    (unsupported interpreter / not installed / wrong version) should
    construct ``NativeLeidenProvider().partition(...)`` and catch the
    specific exception type instead of relying on this boolean."""
    try:
        _check_interpreter_supported()
    except NativeProviderUnsupportedInterpreterError:
        return False
    try:
        import graspologic_native  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        installed_version = importlib.metadata.version(NATIVE_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return False
    return installed_version == NATIVE_PACKAGE_VERSION


__all__ = [
    "CommunityProvider",
    "NativeLeidenProvider",
    "NativeProviderUnavailableError",
    "NativeProviderUnsupportedInterpreterError",
    "NativeProviderVersionMismatchError",
    "is_native_available",
]
