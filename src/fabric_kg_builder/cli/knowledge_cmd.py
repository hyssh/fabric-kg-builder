"""knowledge -- CLI lifecycle commands for M7 knowledge sources, bases, and Data Agents.

AGK-011: Registered under `fabric-kg knowledge` group.  Provides:

  * knowledge source upsert   -- create/update a Search-index knowledge source
  * knowledge base upsert     -- create/update a knowledge base
  * knowledge da upsert       -- create/update a Fabric Data Agent
  * knowledge probe           -- retrieval probe against a knowledge base

All commands:
  - accept configuration from CLI options and environment variables.
  - emit stable JSON output (one JSON object per line or wrapped).
  - fail explicitly (non-zero exit code) for unsupported preview capability.
  - never log or emit credential values.

Environment variables (auto-mapped via Click FABRIC_KG prefix from main.py):
  FABRIC_KG_SEARCH_ENDPOINT  -- Search service endpoint URL
  FABRIC_KG_SEARCH_API_KEY   -- API key for Search (mutually exclusive with token auth)
  FABRIC_KG_FABRIC_TOKEN     -- Bearer token for Fabric API calls
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import click


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _search_endpoint_from_env() -> str:
    return os.environ.get("FABRIC_KG_SEARCH_ENDPOINT", "").strip()


def _fabric_token_from_env() -> str:
    return os.environ.get("FABRIC_KG_FABRIC_TOKEN", "").strip()


def _emit_json(obj: dict[str, Any]) -> None:
    """Write a single JSON object to stdout."""
    click.echo(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _exit_error(msg: str, code: int = 1) -> None:
    """Print error to stderr and exit with non-zero code."""
    click.echo(f"ERROR: {msg}", err=True)
    sys.exit(code)


def _build_transport(dry_run: bool = False):
    from fabric_kg_builder.knowledge.transport import FakeTransport, RequestsTransport  # noqa: PLC0415
    if dry_run:
        return FakeTransport()
    return RequestsTransport()


# ---------------------------------------------------------------------------
# Main knowledge group
# ---------------------------------------------------------------------------


_KNOWLEDGE_EPILOG = """\b
Recommended order after Search indexing:
  1. knowledge source upsert  Bind a deployed Search index or stable alias.
  2. knowledge base upsert    Compose one or more knowledge sources.
  3. knowledge probe          Verify retrieval and citations before agents.

For large document sets, create the index first with:
  fabric-kg deploy-search --env dev --indexes kg-chunks --integrated-vectorization --no-mock

PowerShell example:
\b
  fabric-kg knowledge source upsert --name facilities-source --index-name kg-facilities-kg-chunks --semantic-config kg-chunks-semantic --endpoint $env:AZURE_SEARCH_ENDPOINT
  fabric-kg knowledge base upsert --name facilities-kb --sources facilities-source --endpoint $env:AZURE_SEARCH_ENDPOINT
  fabric-kg knowledge probe --kb facilities-kb --query "Which chiller needs service?" --endpoint $env:AZURE_SEARCH_ENDPOINT
"""


@click.group(
    name="knowledge",
    epilog=_KNOWLEDGE_EPILOG,
    context_settings={"max_content_width": 120},
)
@click.pass_context
def knowledge_group(ctx: click.Context) -> None:
    """Create Search knowledge sources/bases, probe them, and manage Data Agents."""
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# knowledge source
# ---------------------------------------------------------------------------


@knowledge_group.group(name="source")
def source_group() -> None:
    """Manage Search knowledge sources."""


_SOURCE_UPSERT_EPILOG = """\b
Run after deploy-search completes. --index-name accepts the deployed index name
or a stable alias. Prefer an alias when release cutover is managed separately.

PowerShell example:
\b
  fabric-kg knowledge source upsert --name facilities-source --index-name kg-facilities-kg-chunks --semantic-config kg-chunks-semantic --source-fields chunk_id,content,entity_ids,evidence_ids --search-fields content --endpoint $env:AZURE_SEARCH_ENDPOINT
"""


@source_group.command(
    name="upsert",
    epilog=_SOURCE_UPSERT_EPILOG,
    context_settings={"max_content_width": 120},
)
@click.option("--name", required=True, help="Knowledge source name.")
@click.option("--index-name", required=True, help="AI Search index or alias name.")
@click.option("--semantic-config", default=None, help="Semantic configuration name.")
@click.option("--endpoint", default=None, envvar="FABRIC_KG_SEARCH_ENDPOINT",
              help="Search service endpoint URL.")
@click.option("--api-key", default=None, envvar="FABRIC_KG_SEARCH_API_KEY",
              help="Search API key (or use bearer token auth).")
@click.option("--source-fields", default="", help="Comma-separated source data field names.")
@click.option("--search-fields", default="", help="Comma-separated search field names.")
@click.option("--description", default="", help="Optional description.")
@click.option("--dry-run", is_flag=True, default=False, help="Show plan without calling the API.")
@click.pass_context
def source_upsert(
    ctx: click.Context,
    name: str,
    index_name: str,
    semantic_config: str | None,
    endpoint: str | None,
    api_key: str | None,
    source_fields: str,
    search_fields: str,
    description: str,
    dry_run: bool,
) -> None:
    """Create or update a Search-index knowledge source."""
    ep = endpoint or _search_endpoint_from_env()
    if not ep:
        _exit_error("--endpoint or FABRIC_KG_SEARCH_ENDPOINT is required.")

    from fabric_kg_builder.knowledge.models import CapabilityResult, AgentFeature  # noqa: PLC0415
    from fabric_kg_builder.knowledge.search_kb import (  # noqa: PLC0415
        SearchIndexKnowledgeSourceSpec, SearchKbClient,
    )

    src_fields = [f.strip() for f in source_fields.split(",") if f.strip()]
    sch_fields = [f.strip() for f in search_fields.split(",") if f.strip()]
    spec = SearchIndexKnowledgeSourceSpec(
        name=name,
        search_index_name=index_name,
        semantic_configuration_name=semantic_config,
        source_data_fields=src_fields,
        search_fields=sch_fields,
        description=description,
    )

    if dry_run:
        _emit_json({"dry_run": True, "would_upsert": spec.to_body()})
        return

    from fabric_kg_builder.knowledge.models import _GA_FEATURES  # noqa: PLC0415
    cap = CapabilityResult(
        endpoint=ep,
        api_version="2026-04-01",
        available_features=_GA_FEATURES,
    )
    transport = _build_transport(dry_run)
    client = SearchKbClient(capability=cap, transport=transport, api_key=api_key)
    try:
        result = client.upsert_knowledge_source(spec)
    except Exception as exc:
        _exit_error(str(exc))
        return
    _emit_json({
        "name": result.name,
        "created": result.created,
        "status_code": result.status_code,
        "etag": result.etag,
    })


# ---------------------------------------------------------------------------
# knowledge base
# ---------------------------------------------------------------------------


@knowledge_group.group(name="base")
def base_group() -> None:
    """Manage knowledge bases."""


_BASE_UPSERT_EPILOG = """\b
Create knowledge sources first, then list their names with --sources.
Probe the knowledge base before connecting it to an agent.

PowerShell example:
\b
  fabric-kg knowledge base upsert --name facilities-kb --sources facilities-source --description "Building operations evidence" --endpoint $env:AZURE_SEARCH_ENDPOINT
  fabric-kg knowledge probe --kb facilities-kb --query "Where is chiller 1?" --endpoint $env:AZURE_SEARCH_ENDPOINT
"""


@base_group.command(
    name="upsert",
    epilog=_BASE_UPSERT_EPILOG,
    context_settings={"max_content_width": 120},
)
@click.option("--name", required=True, help="Knowledge base name.")
@click.option("--sources", required=True, help="Comma-separated knowledge source names.")
@click.option("--description", default="", help="Optional description.")
@click.option("--endpoint", default=None, envvar="FABRIC_KG_SEARCH_ENDPOINT",
              help="Search service endpoint URL.")
@click.option("--api-key", default=None, envvar="FABRIC_KG_SEARCH_API_KEY",
              help="Search API key.")
@click.option("--preview", is_flag=True, default=False,
              help="Use preview API version (requires acknowledgement).")
@click.option("--preview-acknowledged", is_flag=True, default=False,
              help="Acknowledge preview terms (required with --preview).")
@click.option("--dry-run", is_flag=True, default=False, help="Show plan without calling the API.")
@click.pass_context
def base_upsert(
    ctx: click.Context,
    name: str,
    sources: str,
    description: str,
    endpoint: str | None,
    api_key: str | None,
    preview: bool,
    preview_acknowledged: bool,
    dry_run: bool,
) -> None:
    """Create or update a knowledge base."""
    if preview and not preview_acknowledged:
        _exit_error(
            "--preview requires --preview-acknowledged to confirm preview terms. "
            "Preview features are not recommended for production.", code=2
        )

    ep = endpoint or _search_endpoint_from_env()
    if not ep:
        _exit_error("--endpoint or FABRIC_KG_SEARCH_ENDPOINT is required.")

    from fabric_kg_builder.knowledge.search_kb import KnowledgeBaseSpec, SearchKbClient  # noqa: PLC0415
    from fabric_kg_builder.knowledge.models import (  # noqa: PLC0415
        CapabilityResult, _GA_FEATURES, _PREVIEW_FEATURES, _GA_VERSION, _PREVIEW_VERSION,
    )

    source_names = [s.strip() for s in sources.split(",") if s.strip()]
    spec = KnowledgeBaseSpec(name=name, knowledge_source_names=source_names, description=description)

    api_version = _PREVIEW_VERSION if (preview and preview_acknowledged) else _GA_VERSION
    features = _PREVIEW_FEATURES if api_version == _PREVIEW_VERSION else _GA_FEATURES

    if dry_run:
        _emit_json({"dry_run": True, "api_version": api_version,
                    "would_upsert": spec.to_body(api_version=api_version)})
        return

    cap = CapabilityResult(
        endpoint=ep,
        api_version=api_version,
        available_features=features,
        is_preview=preview and preview_acknowledged,
    )
    transport = _build_transport()
    client = SearchKbClient(capability=cap, transport=transport, api_key=api_key)
    try:
        result = client.upsert_knowledge_base(spec)
    except Exception as exc:
        _exit_error(str(exc))
        return
    _emit_json({
        "name": result.name,
        "created": result.created,
        "status_code": result.status_code,
        "api_version": api_version,
        "etag": result.etag,
    })


# ---------------------------------------------------------------------------
# knowledge da (Fabric Data Agent)
# ---------------------------------------------------------------------------


@knowledge_group.group(name="da")
def da_group() -> None:
    """Manage Fabric Data Agents."""


@da_group.command(name="upsert")
@click.option("--display-name", required=True, help="Data agent display name.")
@click.option("--workspace-id", required=True, envvar="FABRIC_KG_WORKSPACE_ID",
              help="Fabric workspace GUID.")
@click.option("--instruction", default="", help="System instruction for the agent.")
@click.option("--token", default=None, envvar="FABRIC_KG_FABRIC_TOKEN",
              help="Bearer token for Fabric API.")
@click.option("--dry-run", is_flag=True, default=False, help="Show plan without calling the API.")
@click.pass_context
def da_upsert(
    ctx: click.Context,
    display_name: str,
    workspace_id: str,
    instruction: str,
    token: str | None,
    dry_run: bool,
) -> None:
    """Create or update a Fabric Data Agent."""
    tok = token or _fabric_token_from_env()

    from fabric_kg_builder.knowledge.data_agent import (  # noqa: PLC0415
        DataAgentSpec, FabricDataAgentClient,
    )

    spec = DataAgentSpec(display_name=display_name, instruction=instruction)

    if dry_run:
        from fabric_kg_builder.knowledge.data_agent import build_definition_parts  # noqa: PLC0415
        parts = build_definition_parts(spec)
        _emit_json({"dry_run": True, "workspace_id": workspace_id,
                    "display_name": display_name, "definition_part_count": len(parts)})
        return

    if not tok:
        _exit_error("--token or FABRIC_KG_FABRIC_TOKEN is required for Fabric API calls.")

    from fabric_kg_builder.knowledge.transport import RequestsTransport  # noqa: PLC0415
    transport = RequestsTransport()
    client = FabricDataAgentClient(workspace_id=workspace_id, transport=transport, token=tok)
    try:
        result = client.upsert(spec)
    except Exception as exc:
        _exit_error(str(exc))
        return
    _emit_json({
        "display_name": display_name,
        "item_id": result.item_id,
        "created": result.created,
        "workspace_id": workspace_id,
    })


# ---------------------------------------------------------------------------
# knowledge probe (retrieval probe)
# ---------------------------------------------------------------------------


_PROBE_EPILOG = """\b
Use this gate after knowledge source/base creation and before Data Agent or
Foundry agent publication. A successful probe should return relevant citations.

PowerShell example:
\b
  fabric-kg knowledge probe --kb facilities-kb --query "Which maintenance manual applies to the chiller?" --endpoint $env:AZURE_SEARCH_ENDPOINT
"""


@knowledge_group.command(
    name="probe",
    epilog=_PROBE_EPILOG,
    context_settings={"max_content_width": 120},
)
@click.option("--kb", required=True, help="Knowledge base name.")
@click.option("--query", required=True, help="Retrieval query.")
@click.option("--endpoint", default=None, envvar="FABRIC_KG_SEARCH_ENDPOINT",
              help="Search service endpoint URL.")
@click.option("--api-key", default=None, envvar="FABRIC_KG_SEARCH_API_KEY",
              help="Search API key.")
@click.option("--max-runtime", default=60, show_default=True, help="Max runtime seconds.")
@click.pass_context
def probe(
    ctx: click.Context,
    kb: str,
    query: str,
    endpoint: str | None,
    api_key: str | None,
    max_runtime: int,
) -> None:
    """Issue a retrieval probe against a knowledge base."""
    ep = endpoint or _search_endpoint_from_env()
    if not ep:
        _exit_error("--endpoint or FABRIC_KG_SEARCH_ENDPOINT is required.")

    from fabric_kg_builder.knowledge.retrieve import KnowledgeBaseRetriever, PartialRetrievalError  # noqa: PLC0415
    from fabric_kg_builder.knowledge.transport import RequestsTransport  # noqa: PLC0415

    transport = RequestsTransport()
    retriever = KnowledgeBaseRetriever(
        endpoint=ep,
        kb_name=kb,
        api_version="2026-04-01",
        transport=transport,
        api_key=api_key,
    )
    try:
        result = retriever.retrieve_full(query, max_runtime_seconds=max_runtime)
    except PartialRetrievalError as exc:
        _emit_json({
            "partial": True,
            "answer_text": exc.answer_text,
            "citation_count": len(exc.citations),
            "activity_event_count": len(exc.activity),
        })
        sys.exit(3)
    except Exception as exc:
        _exit_error(str(exc))
        return

    _emit_json({
        "kb": kb,
        "answer_text": result.answer_text[:500] if result.answer_text else "",
        "citation_count": len(result.citations),
        "is_partial": result.is_partial,
        "citations": [
            {
                "citation_id": c.citation_id,
                "source_name": c.source_name,
                "doc_key": c.doc_key,
                "score": c.score,
            }
            for c in result.citations[:10]
        ],
    })
