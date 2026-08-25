"""Production configuration and dependency wiring for the reference API.

Deployed environments fail closed unless inbound authentication and both
downstream data sources are fully configured.  Explicit local development is
the only mode that permits unauthenticated, offline adapters.

Environment variables:
  FABRIC_KG_ENVIRONMENT   — "local" | "dev" | "test" | "prod" (default: "prod")
  FABRIC_KG_LOCAL_DEV     — "true" explicitly opts into AllowAll (local only)
  FABRIC_KG_LOCAL_LIVE    — "true" enables Azure data access with az login in local dev
  FABRIC_KG_TENANT_ID     — Azure AD tenant ID (required in non-local modes)
  FABRIC_KG_AUDIENCE      — API audience URI (required in non-local modes)
  FABRIC_KG_ALLOWED_CALLER_OBJECT_IDS — optional comma-separated caller OIDs
  FABRIC_KG_REQUIRED_APP_ROLE — optional required application role
  FABRIC_KG_SEARCH_ENDPOINT — Azure AI Search endpoint
  FABRIC_KG_KB_INDEX      — Azure AI Search index name
  FABRIC_KG_FABRIC_WORKSPACE_ID — Fabric workspace ID
  FABRIC_KG_GRAPH_MODEL_ID — Fabric GraphModel item ID
  FABRIC_KG_MANAGED_IDENTITY_CLIENT_ID — user-assigned identity client ID
  FABRIC_KG_GRAPH_PREVIEW_ACKNOWLEDGED — must be true outside local mode
  FABRIC_KG_API_VERSION   — app version string (default: "0.2.4")

Security contract:
  - "local" + FABRIC_KG_LOCAL_DEV=true → AllowAllVerifier (development only)
  - all other configurations → EntraAuthVerifier (fails closed at startup)
  - NEVER log the Authorization header value
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class AppConfigError(Exception):
    """Raised at startup when required auth configuration is missing."""


@dataclass(frozen=True)
class AppConfig:
    environment: str
    is_local_dev: bool
    local_live_mode: bool
    tenant_id: str
    audience: str
    version: str
    search_endpoint: str
    kb_index_name: str
    visual_index_name: str
    blob_account_url: str
    blob_container: str
    fabric_workspace_id: str
    graph_model_id: str
    managed_identity_client_id: str
    graph_preview_acknowledged: bool
    fabric_scope: str
    fabric_api_endpoint: str
    allowed_caller_object_ids: tuple[str, ...]
    required_app_role: str
    query_schema_mode: str
    query_schema_path: str
    query_schema_hash: str
    query_authority_hash: str
    domain_contract_hash: str
    approved_max_hops: int | None

    @property
    def live_mode(self) -> bool:
        return not self.is_local_dev


def _is_local_dev() -> bool:
    env = os.environ.get("FABRIC_KG_ENVIRONMENT", "prod").lower()
    flag = os.environ.get("FABRIC_KG_LOCAL_DEV", "").lower()
    return env == "local" and flag == "true"


def _csv_values(name: str) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in os.environ.get(name, "").split(",")
        if value.strip()
    )


def _require_https(name: str, value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise AppConfigError(f"{name} must be an absolute HTTPS URL.")


def load_app_config() -> AppConfig:
    """Load and validate application configuration from environment variables.

    Raises:
        AppConfigError: When not in local-dev mode and tenant/audience missing.
    """
    environment = os.environ.get("FABRIC_KG_ENVIRONMENT", "prod").lower()
    is_local = _is_local_dev()
    local_live_mode = (
        is_local
        and os.environ.get("FABRIC_KG_LOCAL_LIVE", "").lower() == "true"
    )
    tenant_id = os.environ.get("FABRIC_KG_TENANT_ID", "")
    audience = os.environ.get("FABRIC_KG_AUDIENCE", "")
    version = os.environ.get("FABRIC_KG_API_VERSION", "0.2.4")
    search_endpoint = os.environ.get("FABRIC_KG_SEARCH_ENDPOINT", "").rstrip("/")
    kb_index = os.environ.get("FABRIC_KG_KB_INDEX", "")
    visual_index = os.environ.get("FABRIC_KG_VISUAL_INDEX", "")
    blob_account_url = os.environ.get("FABRIC_KG_BLOB_ACCOUNT_URL", "").rstrip("/")
    blob_container = os.environ.get("FABRIC_KG_BLOB_CONTAINER", "")
    workspace_id = os.environ.get("FABRIC_KG_FABRIC_WORKSPACE_ID", "")
    graph_model_id = os.environ.get("FABRIC_KG_GRAPH_MODEL_ID", "")
    identity_client_id = (
        os.environ.get("FABRIC_KG_MANAGED_IDENTITY_CLIENT_ID")
        or os.environ.get("AZURE_CLIENT_ID", "")
    )
    preview_ack = (
        os.environ.get("FABRIC_KG_GRAPH_PREVIEW_ACKNOWLEDGED", "").lower()
        == "true"
    )
    fabric_scope = os.environ.get(
        "FABRIC_KG_FABRIC_SCOPE",
        "https://api.fabric.microsoft.com/.default",
    )
    fabric_api_endpoint = os.environ.get(
        "FABRIC_KG_FABRIC_API_ENDPOINT",
        "https://api.fabric.microsoft.com",
    ).rstrip("/")
    allowed_callers = _csv_values("FABRIC_KG_ALLOWED_CALLER_OBJECT_IDS")
    required_app_role = os.environ.get("FABRIC_KG_REQUIRED_APP_ROLE", "").strip()
    query_schema_mode = os.environ.get(
        "FABRIC_KG_QUERY_SCHEMA_MODE",
        "",
    ).strip()
    query_schema_path = os.environ.get(
        "FABRIC_KG_QUERY_SCHEMA_PATH",
        "",
    ).strip()
    query_schema_hash = os.environ.get(
        "FABRIC_KG_QUERY_SCHEMA_HASH", ""
    ).strip()
    query_authority_hash = os.environ.get(
        "FABRIC_KG_QUERY_AUTHORITY_HASH", ""
    ).strip()
    domain_contract_hash = os.environ.get(
        "FABRIC_KG_DOMAIN_CONTRACT_HASH", ""
    ).strip()
    raw_approved_max_hops = os.environ.get(
        "FABRIC_KG_APPROVED_MAX_HOPS", ""
    ).strip()
    try:
        approved_max_hops = (
            int(raw_approved_max_hops)
            if raw_approved_max_hops
            else None
        )
    except ValueError as exc:
        raise AppConfigError(
            "FABRIC_KG_APPROVED_MAX_HOPS must be an integer."
        ) from exc
    if (
        query_schema_mode == "schema2_bounded"
        and approved_max_hops is not None
        and not 1 <= approved_max_hops <= 4
    ):
        raise AppConfigError(
            "FABRIC_KG_APPROVED_MAX_HOPS must be between 1 and 4."
        )
    if query_schema_mode == "schema2_bounded":
        required_query_authority = {
            "FABRIC_KG_QUERY_SCHEMA_PATH": query_schema_path,
            "FABRIC_KG_QUERY_SCHEMA_HASH": query_schema_hash,
            "FABRIC_KG_QUERY_AUTHORITY_HASH": query_authority_hash,
            "FABRIC_KG_DOMAIN_CONTRACT_HASH": domain_contract_hash,
            "FABRIC_KG_APPROVED_MAX_HOPS": approved_max_hops,
        }
        missing_authority = [
            name
            for name, value in required_query_authority.items()
            if value in {None, ""}
        ]
        if missing_authority:
            raise AppConfigError(
                "Schema-2 runtime authority configuration is incomplete: "
                + ", ".join(missing_authority)
            )
    if not query_schema_mode:
        raise AppConfigError(
            "FABRIC_KG_QUERY_SCHEMA_MODE must be explicitly set to "
            "schema1_compatibility or schema2_bounded."
        )
    if query_schema_mode not in {
        "schema1_compatibility",
        "schema2_bounded",
    }:
        raise AppConfigError(
            "FABRIC_KG_QUERY_SCHEMA_MODE must be schema1_compatibility or "
            "schema2_bounded."
        )

    if not is_local:
        required_values = {
            "FABRIC_KG_TENANT_ID": tenant_id,
            "FABRIC_KG_AUDIENCE": audience,
            "FABRIC_KG_SEARCH_ENDPOINT": search_endpoint,
            "FABRIC_KG_KB_INDEX": kb_index,
            "FABRIC_KG_VISUAL_INDEX": visual_index,
            "FABRIC_KG_BLOB_ACCOUNT_URL": blob_account_url,
            "FABRIC_KG_BLOB_CONTAINER": blob_container,
            "FABRIC_KG_FABRIC_WORKSPACE_ID": workspace_id,
            "FABRIC_KG_GRAPH_MODEL_ID": graph_model_id,
            "FABRIC_KG_MANAGED_IDENTITY_CLIENT_ID or AZURE_CLIENT_ID": identity_client_id,
        }
        missing = [name for name, value in required_values.items() if not value]
        if missing:
            raise AppConfigError(
                f"API startup failed: required runtime environment variables are not set: "
                f"{', '.join(missing)}. "
                f"Set FABRIC_KG_ENVIRONMENT=local and FABRIC_KG_LOCAL_DEV=true "
                f"to use explicit offline local-dev mode."
            )
        if not preview_ack:
            raise AppConfigError(
                "FABRIC_KG_GRAPH_PREVIEW_ACKNOWLEDGED=true is required because "
                "the Fabric GraphModel executeQuery API is a preview feature."
            )
        _require_https("FABRIC_KG_SEARCH_ENDPOINT", search_endpoint)
        _require_https("FABRIC_KG_BLOB_ACCOUNT_URL", blob_account_url)
        _require_https("FABRIC_KG_FABRIC_API_ENDPOINT", fabric_api_endpoint)
        if not fabric_scope.endswith("/.default"):
            raise AppConfigError(
                "FABRIC_KG_FABRIC_SCOPE must be an application scope ending in '/.default'."
            )

    return AppConfig(
        environment=environment,
        is_local_dev=is_local,
        local_live_mode=local_live_mode,
        tenant_id=tenant_id,
        audience=audience,
        version=version,
        search_endpoint=search_endpoint,
        kb_index_name=kb_index,
        visual_index_name=visual_index,
        blob_account_url=blob_account_url,
        blob_container=blob_container,
        fabric_workspace_id=workspace_id,
        graph_model_id=graph_model_id,
        managed_identity_client_id=identity_client_id,
        graph_preview_acknowledged=preview_ack,
        fabric_scope=fabric_scope,
        fabric_api_endpoint=fabric_api_endpoint,
        allowed_caller_object_ids=allowed_callers,
        required_app_role=required_app_role,
        query_schema_mode=query_schema_mode,
        query_schema_path=query_schema_path,
        query_schema_hash=query_schema_hash,
        query_authority_hash=query_authority_hash,
        domain_contract_hash=domain_contract_hash,
        approved_max_hops=approved_max_hops,
    )


def build_auth_verifier(config: AppConfig):
    """Return the appropriate InboundAuthVerifier for this config.

    Local-dev: AllowAllVerifier.
    Production/staging: EntraAuthVerifier.
    """
    from fabric_kg_builder.app.auth import AllowAllVerifier, EntraAuthVerifier

    if config.is_local_dev:
        return AllowAllVerifier()
    return EntraAuthVerifier(
        tenant_id=config.tenant_id,
        audience=config.audience,
        allowed_caller_object_ids=config.allowed_caller_object_ids,
        required_app_role=config.required_app_role or None,
    )


def build_runtime_dependencies(config: AppConfig):
    """Build production Search, visual retrieval, and Fabric Graph adapters."""
    if config.is_local_dev and not config.local_live_mode:
        return None, None, None

    try:
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential  # type: ignore[import]
        from azure.search.documents import SearchClient  # type: ignore[import]
        from azure.storage.blob import BlobServiceClient  # type: ignore[import]
    except ImportError as exc:
        raise AppConfigError(
            "Production runtime dependencies are missing. Install the project "
            "with the 'app' extra and Azure Search/Identity dependencies."
        ) from exc

    from fabric_kg_builder.agent.tools.fabric_data import (
        FabricDataAgentAdapter,
        FabricGraphModelGqlClient,
    )
    from fabric_kg_builder.agent.tools.kb_tool import KnowledgeBaseTool
    from fabric_kg_builder.app.visual_search import VisualSearchTool
    from fabric_kg_builder.azure_identity import default_azure_credential

    credential = (
        default_azure_credential(
            exclude_managed_identity_credential=True
        )
        if config.local_live_mode
        else ManagedIdentityCredential(client_id=config.managed_identity_client_id)
    )
    search_client = SearchClient(
        endpoint=config.search_endpoint,
        index_name=config.kb_index_name,
        credential=credential,
    )
    kb_tool = KnowledgeBaseTool(
        index_name=config.kb_index_name,
        _client=search_client,
        fail_on_error=True,
    )
    visual_tool = VisualSearchTool(
        index_name=config.visual_index_name,
        blob_account_url=config.blob_account_url,
        blob_container=config.blob_container,
        _client=SearchClient(
            endpoint=config.search_endpoint,
            index_name=config.visual_index_name,
            credential=credential,
        ),
        _blob_service_client=BlobServiceClient(
            account_url=config.blob_account_url,
            credential=credential,
        ),
        fail_on_error=True,
    )
    graph_client = FabricGraphModelGqlClient(
        workspace_id=config.fabric_workspace_id,
        graph_model_id=config.graph_model_id,
        credential=credential,
        scope=config.fabric_scope,
        api_endpoint=config.fabric_api_endpoint,
    )
    query_schema = None
    if config.query_schema_mode == "schema2_bounded":
        from fabric_kg_builder.semantic.schemas import (
            PersistedQuerySchema,
            compute_persisted_query_schema_hash,
        )

        try:
            query_schema = PersistedQuerySchema.model_validate_json(
                Path(config.query_schema_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise AppConfigError(
                "Could not load sealed schema-2 persisted query schema."
            ) from exc
        if (
            query_schema.schema_mode != "schema2_bounded"
            or query_schema.authority is None
        ):
            raise AppConfigError(
                "FABRIC_KG_QUERY_SCHEMA_PATH must contain a sealed schema-2 "
                "bounded query schema."
            )
        if (
            query_schema.schema_hash
            != compute_persisted_query_schema_hash(query_schema)
        ):
            raise AppConfigError(
                "Packaged persisted query schema hash does not match its "
                "contents."
            )
        authority = query_schema.authority
        expected_authority = {
            "query schema hash": (
                query_schema.schema_hash,
                config.query_schema_hash,
            ),
            "query authority hash": (
                authority.authority_hash,
                config.query_authority_hash,
            ),
            "domain contract hash": (
                authority.domain_contract_hash,
                config.domain_contract_hash,
            ),
            "approved max hops": (
                authority.approved_max_hops,
                config.approved_max_hops,
            ),
        }
        mismatched = [
            label
            for label, (actual, expected) in expected_authority.items()
            if actual != expected
        ]
        if mismatched:
            raise AppConfigError(
                "Configured schema-2 runtime authority differs from the "
                "packaged persisted query schema: "
                + ", ".join(mismatched)
            )
    return kb_tool, visual_tool, FabricDataAgentAdapter(
        _client=graph_client,
        schema_mode=config.query_schema_mode,
        query_schema=query_schema,
    )
