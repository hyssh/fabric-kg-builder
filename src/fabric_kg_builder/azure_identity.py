"""Azure credentials bound to reviewed live-mutation tenant authority."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any


class TenantBoundCredential:
    """Pass the approved tenant on every token request."""

    def __init__(
        self,
        credential: Any,
        tenant_id: str | None,
        allowed_scopes: frozenset[str] | None = None,
    ) -> None:
        self._credential = credential
        self._tenant_id = tenant_id
        self._allowed_scopes = allowed_scopes

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        if (
            self._allowed_scopes is not None
            and any(scope not in self._allowed_scopes for scope in scopes)
        ):
            raise ValueError(
                "Token scope differs from approved mutation authority."
            )
        if self._tenant_id is not None:
            requested = kwargs.get("tenant_id")
            if requested not in (None, self._tenant_id):
                raise ValueError(
                    "Token tenant differs from approved mutation authority."
                )
            kwargs["tenant_id"] = self._tenant_id
        return self._credential.get_token(*scopes, **kwargs)

    def close(self) -> None:
        close = getattr(self._credential, "close", None)
        if callable(close):
            close()


def approved_tenant_id() -> str | None:
    """Return the validated approved tenant propagated by build-deploy."""
    value = os.environ.get("FABRIC_KG_APPROVED_TENANT_ID", "").strip()
    if not value:
        return None
    return str(uuid.UUID(value))


def approved_token_scopes() -> frozenset[str] | None:
    """Return the exact approved token scope set propagated by build-deploy."""
    value = os.environ.get("FABRIC_KG_APPROVED_TOKEN_SCOPES", "").strip()
    if not value:
        return None
    payload = json.loads(value)
    if not isinstance(payload, list) or not all(
        isinstance(scope, str) and scope.endswith("/.default")
        for scope in payload
    ):
        raise ValueError("Approved token scopes must be a JSON string list.")
    return frozenset(payload)


def default_azure_credential(**kwargs: Any) -> TenantBoundCredential:
    """Create ``DefaultAzureCredential`` constrained to approved authority."""
    from azure.identity import DefaultAzureCredential

    return TenantBoundCredential(
        DefaultAzureCredential(**kwargs),
        approved_tenant_id(),
        approved_token_scopes(),
    )
