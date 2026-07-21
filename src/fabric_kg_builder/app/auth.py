"""app/auth.py — managed-identity outbound auth and injectable Entra/EasyAuth verifier.

Design:
  - Outbound calls (to Foundry, AI Search, Graph API) use ManagedIdentityAuthProvider
    which returns Bearer tokens via the Azure identity SDK.  No embedded secrets.
  - Inbound requests are verified by an InboundAuthVerifier.  The default
    AllowAllVerifier accepts all requests (for local dev / tests).  In production,
    EntraAuthVerifier validates the JWT issued by Azure Entra / EasyAuth.

Never embed connection strings, account keys, or client secrets in this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Outbound auth
# ---------------------------------------------------------------------------


@runtime_checkable
class OutboundAuthProvider(Protocol):
    """Minimal outbound auth protocol: returns a Bearer token for a scope."""

    def get_token(self, scope: str) -> str:
        ...


class ManagedIdentityAuthProvider:
    """Outbound auth via Azure Managed Identity.

    In live environments this wraps ``azure.identity.ManagedIdentityCredential``.
    In tests, inject a ``_credential`` mock that exposes ``.get_token(scope).token``.
    """

    def __init__(
        self,
        *,
        client_id: str | None = None,
        _credential: Any | None = None,
    ) -> None:
        self.client_id = client_id
        self._credential = _credential
        self._live_credential: Any = None

    def _get_live_credential(self) -> Any:
        if self._live_credential is None:
            try:
                from azure.identity import ManagedIdentityCredential  # type: ignore[import]
                self._live_credential = ManagedIdentityCredential(client_id=self.client_id)
            except ImportError as exc:
                raise RuntimeError(
                    "azure-identity is required for ManagedIdentityAuthProvider. "
                    "Install it with: pip install azure-identity"
                ) from exc
        return self._live_credential

    def get_token(self, scope: str) -> str:
        """Return a Bearer token for *scope*."""
        credential = self._credential or self._get_live_credential()
        token = credential.get_token(scope)
        return getattr(token, "token", str(token))


class NoopAuthProvider:
    """Auth provider that returns empty tokens — for offline/test use only."""

    def get_token(self, scope: str) -> str:
        return ""


# ---------------------------------------------------------------------------
# Inbound auth (verifier)
# ---------------------------------------------------------------------------


class InboundAuthVerifier(ABC):
    """Base class for inbound request authentication."""

    @abstractmethod
    def verify(self, authorization_header: str | None) -> dict[str, Any]:
        """Verify the inbound request.

        Args:
            authorization_header: The value of the Authorization header.

        Returns:
            Claims dict (e.g. ``{"sub": "...", "email": "..."}``) on success.

        Raises:
            AuthError: If verification fails.
        """


class AuthError(Exception):
    """Raised when inbound auth verification fails."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class AllowAllVerifier(InboundAuthVerifier):
    """Accepts all requests — used for local development and tests.

    NEVER use in production.
    """

    def verify(self, authorization_header: str | None) -> dict[str, Any]:
        return {"sub": "anonymous", "email": "local@dev"}


class EntraAuthVerifier(InboundAuthVerifier):
    """Validates Bearer JWTs issued by Azure Entra ID (formerly AAD).

    Parameters
    ----------
    tenant_id:
        Azure AD tenant ID.
    audience:
        Expected token audience (the API's application ID URI).
    _jwks_client:
        Injected JWKS client for testing.  If None, uses PyJWT's
        ``PyJWKClient`` with the Entra JWKS endpoint.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        audience: str,
        allowed_caller_object_ids: Iterable[str] = (),
        required_app_role: str | None = None,
        _jwks_client: Any | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.audience = audience
        self.allowed_caller_object_ids = frozenset(allowed_caller_object_ids)
        self.required_app_role = required_app_role
        self._jwks_client = _jwks_client
        self._live_jwks_client: Any = None

    @property
    def allowed_issuers(self) -> frozenset[str]:
        return frozenset(
            {
                f"https://login.microsoftonline.com/{self.tenant_id}/v2.0",
                f"https://sts.windows.net/{self.tenant_id}/",
            }
        )

    def _get_jwks_client(self) -> Any:
        if self._jwks_client:
            return self._jwks_client
        if self._live_jwks_client is None:
            try:
                import jwt as pyjwt  # type: ignore[import]
                jwks_uri = (
                    f"https://login.microsoftonline.com/{self.tenant_id}"
                    "/discovery/v2.0/keys"
                )
                self._live_jwks_client = pyjwt.PyJWKClient(jwks_uri)
            except ImportError as exc:
                raise RuntimeError("PyJWT is required for EntraAuthVerifier.") from exc
        return self._live_jwks_client

    def verify(self, authorization_header: str | None) -> dict[str, Any]:
        if not authorization_header:
            raise AuthError("Missing Authorization header.", status_code=401)
        parts = authorization_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AuthError("Authorization header must be 'Bearer <token>'.", status_code=401)
        token = parts[1]
        try:
            import jwt as pyjwt  # type: ignore[import]
            jwks_client = self._get_jwks_client()
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            unverified = pyjwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_aud": False,
                    "verify_exp": False,
                },
            )
            issuer = unverified.get("iss", "")
            if issuer not in self.allowed_issuers:
                raise AuthError("Token issuer is not allowed for this tenant.", status_code=401)
            claims = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=issuer,
            )
            if self.allowed_caller_object_ids:
                caller_oid = str(claims.get("oid", ""))
                if caller_oid not in self.allowed_caller_object_ids:
                    raise AuthError("Caller object ID is not allowed.", status_code=403)
            if self.required_app_role:
                roles = claims.get("roles") or []
                if not isinstance(roles, list) or self.required_app_role not in roles:
                    raise AuthError("Required application role is missing.", status_code=403)
            return claims
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(f"Token verification failed: {exc}", status_code=401) from exc
