"""knowledge.transport — typed HTTP transport protocol with fake/real implementations.

AGK-001: Provides an injectable, Protocol-based HTTP transport so all knowledge
module operations are testable without network access.  ``FakeTransport`` is the
unit-test double; ``RequestsTransport`` is the production implementation.

Security note: Authorization header values are redacted in all log output so
tokens are never written to disk or surfaced in CI traces.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Sentinel used in redacted log lines
_REDACTED = "***"


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with Authorization values redacted."""
    return {
        k: (_REDACTED if k.lower() == "authorization" else v)
        for k, v in headers.items()
    }


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------


@dataclass
class HttpRequest:
    """An outgoing HTTP request.

    ``body`` is sent as JSON when it is a ``dict`` or ``list``; pass ``None``
    for requests with no body (GET, DELETE).
    """

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    timeout: int = 60


@dataclass
class HttpResponse:
    """An HTTP response received from the server."""

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    elapsed_ms: float = 0.0

    def json(self) -> Any:
        """Return ``body`` if it is already parsed; otherwise JSON-decode it."""
        if isinstance(self.body, (dict, list)):
            return self.body
        import json  # noqa: PLC0415

        return json.loads(self.body)

    def raise_for_status(self) -> None:
        """Raise :class:`HttpError` when ``status_code >= 400``."""
        if self.status_code >= 400:
            raise HttpError(
                self.status_code,
                self.body,
                response_headers=self.headers,
            )


class HttpError(Exception):
    """Raised when a server returns a non-success HTTP status code.

    Never catches broad ``Exception`` to disguise failures as success — callers
    must handle this explicitly.

    Attributes
    ----------
    status_code: int
        The HTTP status code (4xx / 5xx).
    body: Any
        The parsed response body (dict) or raw string.
    """

    def __init__(
        self,
        status_code: int,
        body: Any = None,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.response_headers = response_headers or {}
        msg = f"HTTP {status_code}"
        if isinstance(body, dict):
            detail = body.get("error", body)
            if isinstance(detail, dict):
                code = detail.get("code") or detail.get("errorCode") or ""
                message = (
                    detail.get("message")
                    or detail.get("errorMessage")
                    or detail.get("detail")
                    or ""
                )
                if code or message:
                    msg += f": {code} — {message}"
                elif detail:
                    msg += f": {str(detail)[:200]}"
            else:
                msg += f": {detail}"
        elif body:
            msg += f": {str(body)[:200]}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Transport Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class HttpTransport(Protocol):
    """Structural protocol for HTTP transports.

    Both ``FakeTransport`` and ``RequestsTransport`` satisfy this protocol.
    All knowledge module functions accept ``transport: HttpTransport`` so tests
    can inject a ``FakeTransport`` without any monkey-patching.
    """

    def send(self, req: HttpRequest) -> HttpResponse:
        """Send *req* and return the response.

        Raises :class:`HttpError` on non-success status codes when the
        implementation calls ``raise_for_status()``, or on connection failures.
        """
        ...


# ---------------------------------------------------------------------------
# FakeTransport — deterministic test double
# ---------------------------------------------------------------------------


class FakeTransport:
    """Deterministic, in-memory transport for unit tests.

    Routes are registered as ``(method, url_substring) → HttpResponse``.
    Later registrations take priority over earlier ones (most recent wins).
    Raises ``AssertionError`` if a call arrives with no matching route.

    All calls are recorded in ``self.calls`` for post-hoc assertions::

        t = FakeTransport()
        t.register("GET", "/knowledgesources/", HttpResponse(200, body={"value": []}))
        resp = t.send(HttpRequest("GET", "https://svc.search.windows.net/knowledgesources/ks1"))
        assert resp.status_code == 200
        assert len(t.calls) == 1
    """

    def __init__(self) -> None:
        # (method, url_substring, response)
        self._routes: list[tuple[str, str, HttpResponse]] = []
        self.calls: list[HttpRequest] = []

    def register(self, method: str, url_substring: str, response: HttpResponse) -> "FakeTransport":
        """Register *response* for any request whose URL contains *url_substring*.

        Returns *self* for chaining::

            t.register("PUT", "/knowledgesources/", ...).register("GET", ..., ...)
        """
        self._routes.append((method.upper(), url_substring, response))
        return self

    def send(self, req: HttpRequest) -> HttpResponse:
        """Match *req* against registered routes and return the response.

        Raises ``AssertionError`` if no route matches.
        """
        self.calls.append(req)
        method = req.method.upper()
        for route_method, url_sub, resp in reversed(self._routes):
            if route_method == method and url_sub in req.url:
                logger.debug(
                    "[FakeTransport] %s %s → %s (matched '%s')",
                    method,
                    req.url,
                    resp.status_code,
                    url_sub,
                )
                return resp
        registered = [(m, u) for m, u, _ in self._routes]
        raise AssertionError(
            f"FakeTransport: no route for {method} {req.url!r}. "
            f"Registered routes: {registered}"
        )


# ---------------------------------------------------------------------------
# RequestsTransport — production implementation
# ---------------------------------------------------------------------------


class RequestsTransport:
    """HTTP transport backed by the ``requests`` library.

    Lazy-imports ``requests`` to keep the module importable in environments
    where it is not installed (though it is listed as a dependency).
    """

    def send(self, req: HttpRequest) -> HttpResponse:
        """Execute *req* and return a typed :class:`HttpResponse`.

        Raises :class:`HttpError` on connection errors (status_code=0).
        Does **not** auto-raise on non-2xx; callers call ``raise_for_status()``.
        """
        import requests as _req  # noqa: PLC0415

        logger.debug(
            "[RequestsTransport] %s %s headers=%s",
            req.method.upper(),
            req.url,
            _safe_headers(req.headers),
        )
        start = time.monotonic()
        try:
            resp = _req.request(
                method=req.method.upper(),
                url=req.url,
                headers=req.headers,
                json=req.body if req.body is not None else None,
                timeout=req.timeout,
            )
        except _req.RequestException as exc:
            raise HttpError(0, str(exc)) from exc

        elapsed = (time.monotonic() - start) * 1000.0
        try:
            body: Any = resp.json()
        except Exception:  # noqa: BLE001 — json.JSONDecodeError → keep as text
            body = resp.text

        logger.debug(
            "[RequestsTransport] ← HTTP %s (%.1f ms)",
            resp.status_code,
            elapsed,
        )
        return HttpResponse(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            body=body,
            elapsed_ms=elapsed,
        )
