from __future__ import annotations

import pytest

from fabric_kg_builder.azure_identity import (
    TenantBoundCredential,
    approved_tenant_id,
    approved_token_scopes,
)


class _Credential:
    def __init__(self) -> None:
        self.calls = []

    def get_token(self, *scopes, **kwargs):
        self.calls.append((scopes, kwargs))
        return object()


def test_approved_tenant_id_validates_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "FABRIC_KG_APPROVED_TENANT_ID",
        "00000000-0000-4000-8000-0000000000AA",
    )

    assert approved_tenant_id() == (
        "00000000-0000-4000-8000-0000000000aa"
    )

    monkeypatch.setenv("FABRIC_KG_APPROVED_TENANT_ID", "not-a-guid")
    with pytest.raises(ValueError):
        approved_tenant_id()


def test_tenant_bound_credential_passes_approved_tenant() -> None:
    inner = _Credential()
    bound = TenantBoundCredential(
        inner,
        "00000000-0000-4000-8000-000000000001",
    )

    bound.get_token("https://api.fabric.microsoft.com/.default")

    assert inner.calls[0][1]["tenant_id"].endswith("0001")
    with pytest.raises(ValueError, match="differs"):
        bound.get_token(
            "https://api.fabric.microsoft.com/.default",
            tenant_id="00000000-0000-4000-8000-000000000002",
        )


def test_tenant_bound_credential_enforces_approved_scopes() -> None:
    bound = TenantBoundCredential(
        _Credential(),
        "00000000-0000-4000-8000-000000000001",
        frozenset({"https://ai.azure.com/.default"}),
    )

    bound.get_token("https://ai.azure.com/.default")
    with pytest.raises(ValueError, match="scope differs"):
        bound.get_token("https://management.azure.com/.default")


def test_approved_token_scopes_validates_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "FABRIC_KG_APPROVED_TOKEN_SCOPES",
        '["https://ai.azure.com/.default"]',
    )
    assert approved_token_scopes() == frozenset({
        "https://ai.azure.com/.default"
    })

    monkeypatch.setenv("FABRIC_KG_APPROVED_TOKEN_SCOPES", '{"scope":"bad"}')
    with pytest.raises(ValueError):
        approved_token_scopes()


def test_tenant_bound_credential_preserves_standalone_behavior() -> None:
    inner = _Credential()
    bound = TenantBoundCredential(inner, None)

    bound.get_token("https://search.azure.com/.default")

    assert inner.calls[0][1] == {}
