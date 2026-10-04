"""Tests for graph/community_provider.py — v2 derived-hierarchy native
provider wrapper (additive, independent of legacy graph/community.py).

Focused on defect #7: the provider must not claim compatibility with an
arbitrary installed ``graspologic_native`` distribution merely because the
module imports successfully. It must check the actually-installed version
against the exact pin and fail explicitly on any mismatch.

Every "not installed" / "import fails" scenario below deliberately sets
``sys.modules["graspologic_native"] = None`` rather than merely popping any
existing entry. Per Python's import machinery, a ``None`` value in
``sys.modules`` forces ``import graspologic_native`` to raise
``ImportError`` immediately and deterministically — this is required so
these tests behave identically whether or not the real package happens to
be genuinely installed on the host running the suite (e.g. a maintainer's
Python 3.10-3.13 environment with the real ``leiden`` extra installed for
cross-checking). Simply popping a cache entry and hoping the real package
is absent is an environment-dependent test-isolation defect: on a host
where it IS actually installed, a bare pop lets the real finder succeed
(or, if the previous test left partial submodule state cached, can even
surface a confusing ``NameError`` from the real package's own
``__init__.py`` instead of the intended ``ImportError``).

These tests otherwise simulate "importable" via a fake module object
injected into ``sys.modules`` plus a monkeypatched
``importlib.metadata.version`` — exercising the match/mismatch/not-found
control flow is what's covered here; see the project's own cross-checking
environment for the real end-to-end "really installed at exactly the
pinned version" happy path.
"""

from __future__ import annotations

import importlib.metadata
import sys
from types import SimpleNamespace

import pytest

from fabric_kg_builder.graph.community_contracts import (
    NATIVE_PACKAGE_MAX_PYTHON_EXCLUSIVE,
    NATIVE_PACKAGE_MIN_PYTHON,
    NATIVE_PACKAGE_NAME,
    NATIVE_PACKAGE_VERSION,
    NativeProviderUnavailableError,
    NativeProviderUnsupportedInterpreterError,
)
from fabric_kg_builder.graph.community_provider import (
    NativeLeidenProvider,
    NativeProviderVersionMismatchError,
    is_native_available,
)

pytestmark = pytest.mark.unit

_FAKE_MODULE_NAME = "graspologic_native"
# A fixed, always-in-range stand-in for "whatever interpreter this test
# suite happens to run under" so import/version-match tests exercise their
# own branch deterministically, independent of the actual host Python
# (which may itself be outside graspologic-native's supported range, as
# verified separately by TestUnsupportedInterpreter below).
_SUPPORTED_VERSION_INFO = (3, 12, 5, "final", 0)


@pytest.fixture
def _supported_interpreter(monkeypatch: pytest.MonkeyPatch):
    """Pin the interpreter-support check to a known-supported version so
    import/version-match control flow can be tested regardless of the
    actual host interpreter's own support status."""
    monkeypatch.setattr(sys, "version_info", _SUPPORTED_VERSION_INFO)


def _block_native_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministically force ``import graspologic_native`` to raise
    ``ImportError``, regardless of whether the real package happens to be
    genuinely installed on the host running this suite. See the module
    docstring for why a bare ``sys.modules.pop(...)`` is not sufficient."""
    monkeypatch.setitem(sys.modules, _FAKE_MODULE_NAME, None)


@pytest.fixture(autouse=True)
def _clean_fake_module():
    """Ensure no stray fake (or ``None``-blocked) module from a prior test
    leaks across tests, regardless of whether the real package is actually
    installed on the host running this suite."""
    original = sys.modules.pop(_FAKE_MODULE_NAME, None)
    yield
    if original is not None:
        sys.modules[_FAKE_MODULE_NAME] = original
    else:
        sys.modules.pop(_FAKE_MODULE_NAME, None)


class TestNotInstalled:
    def test_ensure_module_raises_unavailable_when_import_fails(
        self, monkeypatch: pytest.MonkeyPatch, _supported_interpreter
    ) -> None:
        _block_native_import(monkeypatch)
        provider = NativeLeidenProvider()
        with pytest.raises(NativeProviderUnavailableError) as exc_info:
            provider._ensure_module()
        # On a *supported* interpreter, "not installed" must be the plain
        # base error, never misreported as an interpreter mismatch.
        assert not isinstance(exc_info.value, NativeProviderUnsupportedInterpreterError)

    def test_is_native_available_false_when_not_importable(
        self, monkeypatch: pytest.MonkeyPatch, _supported_interpreter
    ) -> None:
        _block_native_import(monkeypatch)
        assert is_native_available() is False


class TestUnsupportedInterpreter:
    """Defect follow-up: selecting the optional 'leiden' extra on an
    interpreter outside graspologic-native==1.2.5's own published
    Requires-Python range (>=3.8,<3.14) must fail explicitly and
    distinctly from "not installed" — no import attempted, no ambiguity."""

    @pytest.mark.parametrize(
        "running",
        [
            (3, 7, 9),  # below NATIVE_PACKAGE_MIN_PYTHON
            (3, 14, 0),  # at/above NATIVE_PACKAGE_MAX_PYTHON_EXCLUSIVE
            (4, 0, 0),  # far above
        ],
    )
    def test_ensure_module_raises_unsupported_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, running: tuple[int, int, int]
    ) -> None:
        monkeypatch.setattr(sys, "version_info", running)
        provider = NativeLeidenProvider()
        with pytest.raises(NativeProviderUnsupportedInterpreterError) as exc_info:
            provider._ensure_module()
        assert isinstance(exc_info.value, NativeProviderUnavailableError)
        message = str(exc_info.value)
        assert "3.10-3.13" in message or "3.10" in message

    @pytest.mark.parametrize("running", [(3, 10, 0), (3, 11, 4), (3, 12, 5), (3, 13, 0)])
    def test_in_range_interpreter_does_not_raise_unsupported(
        self, monkeypatch: pytest.MonkeyPatch, running: tuple[int, int, int]
    ) -> None:
        """In-range interpreters must fall through to the existing
        import-attempt logic (and fail there as "not installed" in this
        offline environment), never as an interpreter mismatch."""
        monkeypatch.setattr(sys, "version_info", running)
        _block_native_import(monkeypatch)
        provider = NativeLeidenProvider()
        with pytest.raises(NativeProviderUnavailableError) as exc_info:
            provider._ensure_module()
        assert not isinstance(exc_info.value, NativeProviderUnsupportedInterpreterError)

    def test_is_native_available_false_without_attempting_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        monkeypatch.setattr(sys, "version_info", (3, 14, 0))
        real_import = builtins.__import__

        def _guard(name, *args, **kwargs):
            if name == _FAKE_MODULE_NAME:
                raise AssertionError("import attempted on unsupported interpreter")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _guard)
        assert is_native_available() is False

    def test_boundary_min_supported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NATIVE_PACKAGE_MIN_PYTHON itself is inclusive (>=3.8)."""
        monkeypatch.setattr(sys, "version_info", NATIVE_PACKAGE_MIN_PYTHON + (0,))
        _block_native_import(monkeypatch)
        provider = NativeLeidenProvider()
        with pytest.raises(NativeProviderUnavailableError) as exc_info:
            provider._ensure_module()
        assert not isinstance(exc_info.value, NativeProviderUnsupportedInterpreterError)

    def test_boundary_max_exclusive_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NATIVE_PACKAGE_MAX_PYTHON_EXCLUSIVE itself is exclusive (<3.14)."""
        monkeypatch.setattr(sys, "version_info", NATIVE_PACKAGE_MAX_PYTHON_EXCLUSIVE + (0,))
        provider = NativeLeidenProvider()
        with pytest.raises(NativeProviderUnsupportedInterpreterError):
            provider._ensure_module()


class TestVersionMismatch:
    def test_ensure_module_raises_version_mismatch_on_wrong_installed_version(
        self, monkeypatch: pytest.MonkeyPatch, _supported_interpreter
    ) -> None:
        sys.modules[_FAKE_MODULE_NAME] = SimpleNamespace()
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")

        provider = NativeLeidenProvider()
        with pytest.raises(NativeProviderVersionMismatchError) as exc_info:
            provider._ensure_module()

        assert exc_info.value.installed_version == "9.9.9"
        assert exc_info.value.expected_version == NATIVE_PACKAGE_VERSION
        # Must subclass NativeProviderUnavailableError so every existing
        # "unavailable => explicit fail, no fallback" caller is covered.
        assert isinstance(exc_info.value, NativeProviderUnavailableError)

    def test_version_mismatch_error_never_claims_compatibility(
        self, monkeypatch: pytest.MonkeyPatch, _supported_interpreter
    ) -> None:
        sys.modules[_FAKE_MODULE_NAME] = SimpleNamespace()
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.1")

        provider = NativeLeidenProvider()
        with pytest.raises(NativeProviderVersionMismatchError) as exc_info:
            provider._ensure_module()
        message = str(exc_info.value)
        assert "0.0.1" in message
        assert NATIVE_PACKAGE_VERSION in message

    def test_is_native_available_false_on_wrong_installed_version(
        self, monkeypatch: pytest.MonkeyPatch, _supported_interpreter
    ) -> None:
        """Availability must align with the exact pin, not merely
        importability — a wrong installed version is not available."""
        sys.modules[_FAKE_MODULE_NAME] = SimpleNamespace()
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")
        assert is_native_available() is False


class TestPackageNotFoundMetadataAvailability:
    def test_is_native_available_false_when_metadata_missing(
        self, monkeypatch: pytest.MonkeyPatch, _supported_interpreter
    ) -> None:
        """An importable module whose distribution metadata cannot be
        found must not be reported as available."""
        sys.modules[_FAKE_MODULE_NAME] = SimpleNamespace()

        def _raise_not_found(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", _raise_not_found)
        assert is_native_available() is False


class TestVersionMatch:
    def test_ensure_module_succeeds_when_installed_version_matches_pin(
        self, monkeypatch: pytest.MonkeyPatch, _supported_interpreter
    ) -> None:
        fake_module = SimpleNamespace(hierarchical_leiden=lambda **kwargs: [])
        sys.modules[_FAKE_MODULE_NAME] = fake_module
        monkeypatch.setattr(importlib.metadata, "version", lambda name: NATIVE_PACKAGE_VERSION)

        provider = NativeLeidenProvider()
        resolved = provider._ensure_module()
        assert resolved is fake_module

    def test_is_native_available_true_when_installed_version_matches_pin(
        self, monkeypatch: pytest.MonkeyPatch, _supported_interpreter
    ) -> None:
        sys.modules[_FAKE_MODULE_NAME] = SimpleNamespace(hierarchical_leiden=lambda **kwargs: [])
        monkeypatch.setattr(importlib.metadata, "version", lambda name: NATIVE_PACKAGE_VERSION)
        assert is_native_available() is True

    def test_ensure_module_is_cached_after_first_success(
        self, monkeypatch: pytest.MonkeyPatch, _supported_interpreter
    ) -> None:
        fake_module = SimpleNamespace(hierarchical_leiden=lambda **kwargs: [])
        sys.modules[_FAKE_MODULE_NAME] = fake_module
        call_count = {"n": 0}

        def _version(name: str) -> str:
            call_count["n"] += 1
            return NATIVE_PACKAGE_VERSION

        monkeypatch.setattr(importlib.metadata, "version", _version)

        provider = NativeLeidenProvider()
        provider._ensure_module()
        provider._ensure_module()
        assert call_count["n"] == 1


class TestPackageNotFoundMetadata:
    def test_ensure_module_raises_unavailable_when_metadata_missing(
        self, monkeypatch: pytest.MonkeyPatch, _supported_interpreter
    ) -> None:
        """An importable module whose distribution metadata cannot be found
        (e.g. a differently-packaged/vendored module shadowing the real
        name) must not be treated as a silent version match — treat as
        unavailable rather than guess."""
        sys.modules[_FAKE_MODULE_NAME] = SimpleNamespace()

        def _raise_not_found(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", _raise_not_found)

        provider = NativeLeidenProvider()
        with pytest.raises(NativeProviderUnavailableError):
            provider._ensure_module()


class TestProviderIdentityIntrospection:
    def test_provider_id_and_version_available_without_import(self) -> None:
        """Constructing the provider (for e.g. identity/version
        introspection) must never require the native dependency."""
        provider = NativeLeidenProvider()
        assert provider.provider_id == "leiden_native"
        assert provider.provider_version == NATIVE_PACKAGE_VERSION
        assert NATIVE_PACKAGE_NAME  # sanity: constant is non-empty
