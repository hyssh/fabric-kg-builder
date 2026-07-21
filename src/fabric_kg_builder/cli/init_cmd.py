"""init command — scaffold a new fabric-kg-builder project."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NamedTuple

import click
import yaml

from fabric_kg_builder.domain import default_domain_contract, render_domain_contract_yaml


_INIT_EPILOG = """\b
Examples:
  fabric-kg init
  fabric-kg init --target ./my-project
  fabric-kg init --template csv-only
  fabric-kg init --force

Exit codes: 0 success · 1 error · 2 already initialized (no-op).

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""

# ─────────────────────────────────────────────────────────────────────────────
# Scaffold templates — domain-neutral; no Surface/Device/Symptom types
# ─────────────────────────────────────────────────────────────────────────────

_FABRIC_KG_YAML = """\
# fabric-kg.yaml — NON-SECRET project configuration.
# Secrets (API keys, connection strings, SAS tokens) are NEVER stored here.
# Use .env (see .env.example) and ontology/environments/{env}.json for resource IDs.

foundry:
  # Azure AI Foundry project endpoint — provided via .env (secret-adjacent).
  endpoint: ${AZURE_AI_FOUNDRY_ENDPOINT}
  # Azure OpenAI endpoint for the AzureOpenAI SDK.
  openai_endpoint: ${AZURE_OPENAI_ENDPOINT}
  project: ${FOUNDRY_PROJECT_NAME}

enrichment:
  # Maximum concurrent LLM calls. Use 1 for sequential fallback.
  max_concurrent: 4
  # Chat model deployment — gpt-4.1 @ >=200K TPM.
  chat_deployment: gpt-4.1
  # Embedding model deployment — text-embedding-3-large (1536 dims).
  embedding_deployment: text-embedding-3-large
  # LOCKED: must match the AI Search chunk_vector field width. Reindex if changed.
  embedding_dimensions: 1536
  # Vision uses the chat deployment (multimodal) by default.
  vision_deployment: gpt-4.1

blob_storage:
  container: kg-assets

search:
  enabled: true
  index_prefix: kg-

document_intelligence:
  endpoint: ${AZURE_DOCINTEL_ENDPOINT}
"""

_FABRIC_KG_YAML_CSV_ONLY = """\
# fabric-kg.yaml — NON-SECRET project configuration (csv-only template).
# PDF/DOCX document intelligence is not required for this template.
# Secrets and resource IDs are never stored here.

foundry:
  endpoint: ${AZURE_AI_FOUNDRY_ENDPOINT}
  openai_endpoint: ${AZURE_OPENAI_ENDPOINT}
  project: ${FOUNDRY_PROJECT_NAME}

enrichment:
  max_concurrent: 4
  chat_deployment: gpt-4.1
  embedding_deployment: text-embedding-3-large
  embedding_dimensions: 1536
  vision_deployment: gpt-4.1

blob_storage:
  container: kg-assets

search:
  enabled: true
  index_prefix: kg-

# Source adapters — csv-only template enables CSV, disables PDF/DOCX.
source_adapters:
  csv:
    enabled: true
  pdf:
    enabled: false
  docx:
    enabled: false
"""

_ENV_EXAMPLE = """\
# .env.example — copy to .env and fill in real values.
# NEVER commit .env (it is gitignored).
# With DefaultAzureCredential (dev), keys are optional when 'az login' grants RBAC.

# --- Azure AI Foundry (required) ---
AZURE_AI_FOUNDRY_ENDPOINT=https://<account>.services.ai.azure.com/api/projects/<project>
FOUNDRY_PROJECT_NAME=<your-project>
# AZURE_AI_FOUNDRY_API_KEY=        # optional when using DefaultAzureCredential

# --- Azure OpenAI (AzureOpenAI SDK endpoint) ---
AZURE_OPENAI_ENDPOINT=https://<account>.openai.azure.com/
# AZURE_OPENAI_API_KEY=            # optional when using DefaultAzureCredential

# --- Azure AI Document Intelligence ---
AZURE_DOCINTEL_ENDPOINT=https://<resource>.cognitiveservices.azure.com
# AZURE_DOCINTEL_API_KEY=

# --- Azure AI Search ---
AZURE_SEARCH_ENDPOINT=https://<service>.search.windows.net
# AZURE_SEARCH_API_KEY=

# --- Azure Blob Storage (visual assets) ---
# DefaultAzureCredential is preferred. Account key as fallback:
# AZURE_STORAGE_KEY=

# --- Service Principal (CI/prod only; dev uses az login) ---
# FABRIC_CLIENT_ID=
# FABRIC_CLIENT_SECRET=
# FABRIC_TENANT_ID=
"""


def _infra_env_yaml(env: str) -> str:
    """Return an infra/environments/{env}.yaml scaffold with env-specific values."""
    tpm = {"dev": 200000, "test": 100000, "prod": 400000}[env]
    return f"""\
# infra/environments/{env}.yaml — NON-SECRET infrastructure manifest.
# Set AZURE_SUBSCRIPTION_ID and FABRIC_CAPACITY_ID in your environment.
# Secrets must never be stored here — use .env.
# SPEC-006 §5.1 / INF-001.

schema_version: "1.0"
environment: {env}

azure:
  subscription_id: ${{AZURE_SUBSCRIPTION_ID}}
  resource_group:
    mode: connect          # connect = use existing RG; create = provision a new one
    name: <your-resource-group>
  default_location: eastus2
  tags:
    application: fabric-kg-builder
    environment: {env}

identity:
  mode: user-assigned     # user-assigned | system-assigned | none

resources:
  storage:
    mode: create
    name: null              # null = auto-generate a deterministic name
    hierarchical_namespace: true
    container: kg-assets
    retention_days: 365

  document_intelligence:
    mode: create
    name: null
    sku: S0

  foundry:
    mode: create
    name: null
    project_name: kg-{env}
    models:
      chat:
        model: gpt-4.1
        sku: GlobalStandard
        # target_tpm must be a multiple of 1000.
        target_tpm: {tpm}
      embedding:
        model: text-embedding-3-large
        dimensions: 1536

  search:
    mode: create
    name: null
    sku: standard
    semantic_ranker: standard  # requires Standard SKU or higher

fabric:
  capacity_id: ${{FABRIC_CAPACITY_ID}}
  workspace:
    mode: create
    name: kg-{env}
  lakehouse:
    mode: create
    name: kg
    enable_schemas: true
  ontology:
    mode: create
    display_name: KG Ontology
  graph_model:
    mode: create
    display_name: KG Graph

features:
  foundry_iq: false
  fabric_data_agent: false
  graph: false
  reference_app: false
"""


def _ontology_env_json(env: str) -> str:
    """Return an ontology/environments/{env}.json scaffold template."""
    data = {
        "_comment": (
            f"NON-SECRET environment config template for the {env.upper()} environment. "
            "Fill in your values. Secrets (API keys, connection strings) go in .env — never here."
        ),
        "env": env,
        "auth_strategy": "DefaultAzureCredential",
        "azure": {
            "subscription_id": "<your-subscription-id>",
        },
        "fabric": {
            "workspace_id": "<your-fabric-workspace-id>",
            "workspace_display_name": "<your-fabric-workspace-name>",
            "lakehouse_item_id": "<your-lakehouse-item-id>",
            "lakehouse_display_name": "kg_lakehouse",
            "ontology_item_id": "<your-ontology-item-id>",
            "ontology_display_name": "KG_Ontology",
            "graph_model_item_id": "<your-graph-model-item-id>",
            "graph_model_display_name": "KG Graph",
            "data_agent_item_id": "<your-data-agent-item-id>",
            "data_agent_display_name": f"fkg-{env}-data-agent",
            "onelake_tables_path": (
                "https://onelake.dfs.fabric.microsoft.com"
                "/<workspace-id>/<lakehouse-item-id>/Tables"
            ),
            "sql_endpoint": (
                "<your-lakehouse-sql-endpoint>.datawarehouse.fabric.microsoft.com"
            ),
            "schemas_enabled": True,
            "schema_name": "dbo",
        },
        "ai_search": {
            "enabled": True,
            "endpoint": "https://<your-search-service>.search.windows.net",
            "index_prefix": f"kg-{env}-",
            "index_chunks": "kg-chunks",
            "index_document_elements": "kg-document-elements",
            "index_visual_assets": "kg-visual-assets",
        },
    }
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Domain-neutral ontology scaffold (no Surface/Device/Symptom/Cause/Resolution)
# ---------------------------------------------------------------------------

# KGEntity uses the first numeric slot in each range; infrastructure types keep
# their canonical IDs so they stay stable if this project is later extended.
_GENERIC_IDS_LOCK: dict = {
    "schema_version": "1.0",
    "entity_types": {
        "KGEntity": {
            "semantic_id": "entity-type:kg-entity",
            "fabric_id": "1000000000000000001",
        },
        "DocumentChunk": {
            "semantic_id": "entity-type:document-chunk",
            "fabric_id": "1000000000000000101",
        },
        "Figure": {
            "semantic_id": "entity-type:figure",
            "fabric_id": "1000000000000000108",
        },
        "ImageAsset": {
            "semantic_id": "entity-type:image-asset",
            "fabric_id": "1000000000000000112",
        },
        "SearchIndexRecord": {
            "semantic_id": "entity-type:search-index-record",
            "fabric_id": "1000000000000000203",
        },
    },
    "relationship_types": {
        "related_to": {
            "semantic_id": "relationship-type:related-to",
            "fabric_id": "2000000000000000001",
        },
        "evidenced_by": {
            "semantic_id": "relationship-type:evidenced-by",
            "fabric_id": "2000000000000000108",
        },
        "shown_in": {
            "semantic_id": "relationship-type:shown-in",
            "fabric_id": "2000000000000000109",
        },
        "indexed_as": {
            "semantic_id": "relationship-type:indexed-as",
            "fabric_id": "2000000000000000117",
        },
    },
}

_GENERIC_SEMANTIC_CONTRACT: dict = {
    "schema_version": "1.0",
    "contract_version": "0.1.0-draft",
    "name": "Draft project semantics",
    "description": (
        "Domain-neutral starter semantics. Replace KGEntity and generic "
        "relationships with precise business concepts before approval."
    ),
    "entity_types": [
        {
            "id": "entity-type:kg-entity",
            "name": "KGEntity",
            "business_name": "Domain entity",
            "description": (
                "Draft placeholder for a domain-specific business entity. "
                "Replace this type before approving the contract."
            ),
            "identifiers": ["entity_id"],
            "aliases": [],
            "properties": [
                {"name": "entity_id", "type": "string", "required": True},
                {"name": "display_name", "type": "string", "required": True},
                {"name": "canonical_key", "type": "string", "required": True},
                {"name": "search_aliases", "type": "string"},
                {"name": "entity_type", "type": "string"},
                {"name": "description", "type": "string"},
            ],
            "lineage_properties": [
                "project_id",
                "asset_id",
                "asset_version_id",
                "run_id",
            ],
            "publication_status": "experimental",
        },
        {
            "id": "entity-type:document-chunk",
            "name": "DocumentChunk",
            "business_name": "Document chunk",
            "description": "A source-backed text unit used for evidence retrieval.",
            "identifiers": ["chunk_id"],
            "aliases": [],
            "properties": [
                {"name": "chunk_id", "type": "string", "required": True},
                {"name": "content", "type": "string", "required": True},
                {"name": "related_entity_ids", "type": "string"},
                {"name": "entity_search_keys", "type": "string"},
            ],
            "lineage_properties": [
                "project_id",
                "asset_id",
                "asset_version_id",
                "run_id",
            ],
            "publication_status": "optional",
        },
        {
            "id": "entity-type:figure",
            "name": "Figure",
            "business_name": "Figure",
            "description": "A figure or diagram extracted from a source asset.",
            "identifiers": ["document_element_id"],
            "aliases": [],
            "properties": [
                {
                    "name": "document_element_id",
                    "type": "string",
                    "required": True,
                },
                {"name": "title", "type": "string"},
                {"name": "blob_url", "type": "uri"},
            ],
            "lineage_properties": [
                "project_id",
                "asset_id",
                "asset_version_id",
                "run_id",
            ],
            "publication_status": "optional",
        },
        {
            "id": "entity-type:image-asset",
            "name": "ImageAsset",
            "business_name": "Image asset",
            "description": "A visual asset retained in immutable blob storage.",
            "identifiers": ["image_id"],
            "aliases": [],
            "properties": [
                {"name": "image_id", "type": "string", "required": True},
                {"name": "caption", "type": "string"},
                {"name": "blob_url", "type": "uri", "required": True},
            ],
            "lineage_properties": [
                "project_id",
                "asset_id",
                "asset_version_id",
                "run_id",
            ],
            "publication_status": "optional",
        },
        {
            "id": "entity-type:search-index-record",
            "name": "SearchIndexRecord",
            "business_name": "Search index record",
            "description": "A retrieval record linked to canonical graph identifiers.",
            "identifiers": ["search_record_id"],
            "aliases": [],
            "properties": [
                {
                    "name": "search_record_id",
                    "type": "string",
                    "required": True,
                },
                {"name": "display_name", "type": "string", "required": True},
            ],
            "lineage_properties": [
                "project_id",
                "asset_id",
                "asset_version_id",
                "run_id",
            ],
            "publication_status": "optional",
        },
    ],
    "relationship_types": [
        {
            "id": "relationship-type:related-to",
            "predicate": "related_to",
            "business_name": "related to",
            "description": (
                "Draft generic relationship. Replace it with precise directed "
                "business predicates before approval."
            ),
            "source_type": "entity-type:kg-entity",
            "target_type": "entity-type:kg-entity",
            "direction": "source_to_target",
            "evidence_policy": "required_for_asserted",
            "assertion_policy": {
                "allowed_statuses": ["asserted", "unresolved"],
                "default_status": "unresolved",
            },
            "temporal": "optional",
            "publication_status": "experimental",
        },
        {
            "id": "relationship-type:evidenced-by",
            "predicate": "evidenced_by",
            "business_name": "evidenced by",
            "description": "Links a business entity to supporting source content.",
            "source_type": "entity-type:kg-entity",
            "target_type": "entity-type:document-chunk",
            "direction": "source_to_target",
            "evidence_policy": "required_for_asserted",
            "assertion_policy": {
                "allowed_statuses": ["asserted", "unresolved"],
                "default_status": "unresolved",
            },
            "temporal": "optional",
            "publication_status": "optional",
        },
        {
            "id": "relationship-type:shown-in",
            "predicate": "shown_in",
            "business_name": "shown in",
            "description": "Links a business entity to a source figure.",
            "source_type": "entity-type:kg-entity",
            "target_type": "entity-type:figure",
            "direction": "source_to_target",
            "evidence_policy": "required_for_asserted",
            "assertion_policy": {
                "allowed_statuses": ["asserted", "unresolved"],
                "default_status": "unresolved",
            },
            "temporal": "not_applicable",
            "publication_status": "optional",
        },
        {
            "id": "relationship-type:indexed-as",
            "predicate": "indexed_as",
            "business_name": "indexed as",
            "description": "Links source content to its Search retrieval record.",
            "source_type": "entity-type:document-chunk",
            "target_type": "entity-type:search-index-record",
            "direction": "source_to_target",
            "evidence_policy": "required_for_asserted",
            "assertion_policy": {
                "allowed_statuses": ["asserted", "unresolved"],
                "default_status": "unresolved",
            },
            "temporal": "not_applicable",
            "publication_status": "optional",
        },
    ],
    "approval": {
        "status": "draft",
        "notes": [
            "Replace generic semantics with the approved domain model before compilation."
        ],
    },
    "metadata": {"scaffold": "domain-neutral"},
}

_GENERIC_SEMANTIC_MAPPINGS: dict = {
    "schema_version": "1.0",
    "entity_types": [
        {
            "semantic_id": "entity-type:kg-entity",
            "table": "entities",
            "entity_id_column": "entity_id",
            "display_name_column": "display_name",
            "property_columns": {
                "entity_id": "entity_id",
                "display_name": "display_name",
                "canonical_key": "canonical_key",
                "search_aliases": "search_aliases",
                "entity_type": "entity_type",
                "description": "description",
            },
            "type_filter_column": "entity_type",
            "type_filter_value": "KGEntity",
        },
        {
            "semantic_id": "entity-type:document-chunk",
            "table": "chunks",
            "entity_id_column": "chunk_id",
            "display_name_column": "content",
            "property_columns": {
                "chunk_id": "chunk_id",
                "content": "content",
                "related_entity_ids": "related_entity_ids",
                "entity_search_keys": "entity_search_keys",
            },
        },
        {
            "semantic_id": "entity-type:figure",
            "table": "document_elements",
            "entity_id_column": "document_element_id",
            "display_name_column": "title",
            "property_columns": {
                "document_element_id": "document_element_id",
                "title": "title",
                "blob_url": "blob_url",
            },
        },
        {
            "semantic_id": "entity-type:image-asset",
            "table": "visual_assets",
            "entity_id_column": "image_id",
            "display_name_column": "caption",
            "property_columns": {
                "image_id": "image_id",
                "caption": "caption",
                "blob_url": "blob_url",
            },
        },
        {
            "semantic_id": "entity-type:search-index-record",
            "table": "chunks",
            "entity_id_column": "chunk_id",
            "display_name_column": "chunk_id",
            "property_columns": {
                "search_record_id": "chunk_id",
                "display_name": "chunk_id",
            },
        },
    ],
    "relationship_types": [
        {
            "semantic_id": "relationship-type:related-to",
            "table": "relationships",
            "relationship_id_column": "relationship_id",
            "source_entity_id_column": "source_entity_id",
            "target_entity_id_column": "target_entity_id",
            "evidence_id_column": "evidence_id",
            "type_filter_column": "relationship_type",
            "type_filter_value": "related_to",
        },
        {
            "semantic_id": "relationship-type:evidenced-by",
            "table": "relationships",
            "relationship_id_column": "relationship_id",
            "source_entity_id_column": "source_entity_id",
            "target_entity_id_column": "target_entity_id",
            "evidence_id_column": "evidence_id",
            "type_filter_column": "relationship_type",
            "type_filter_value": "evidenced_by",
        },
        {
            "semantic_id": "relationship-type:shown-in",
            "table": "relationships",
            "relationship_id_column": "relationship_id",
            "source_entity_id_column": "source_entity_id",
            "target_entity_id_column": "target_entity_id",
            "evidence_id_column": "evidence_id",
            "type_filter_column": "relationship_type",
            "type_filter_value": "shown_in",
        },
        {
            "semantic_id": "relationship-type:indexed-as",
            "table": "relationships",
            "relationship_id_column": "relationship_id",
            "source_entity_id_column": "source_entity_id",
            "target_entity_id_column": "target_entity_id",
            "evidence_id_column": "evidence_id",
            "type_filter_column": "relationship_type",
            "type_filter_value": "indexed_as",
        },
    ],
}

_GENERIC_SEMANTIC_VOCABULARY: dict = {
    "schema_version": "1.0",
    "terms": [
        {
            "id": "term:domain-entity",
            "preferred_label": "Domain entity",
            "definition": "A placeholder to be replaced with a precise business concept.",
            "aliases": [],
        },
        {
            "id": "term:source-evidence",
            "preferred_label": "Source evidence",
            "definition": "Immutable source content that supports a graph assertion.",
            "aliases": ["evidence"],
        },
    ],
}

_GENERIC_COMPETENCY_SUITE: dict = {
    "schema_version": "1.0",
    "cases": [
        {
            "id": "replace-with-domain-competency",
            "question": (
                "Which related domain entities are supported by source evidence?"
            ),
            "semantic_plan": {
                "intent": "find_evidence_supported_relationships",
                "requested_concepts": [
                    "entity",
                    "relationship",
                    "evidence",
                ],
                "required_types": ["entity-type:kg-entity"],
                "required_relationships": [
                    "relationship-type:related-to"
                ],
                "optional_relationships": [],
                "requested_properties": [],
                "evidence_required": True,
                "path_steps": [
                    {
                        "step_id": "related-entities",
                        "from_type_id": "entity-type:kg-entity",
                        "via_relationship_id": (
                            "relationship-type:related-to"
                        ),
                        "to_type_id": "entity-type:kg-entity",
                        "direction": "source_to_target",
                        "optional": False,
                    }
                ],
                "budget": {
                    "max_hops": 4,
                    "max_nodes": 6,
                    "max_relationships": 5,
                    "max_rows_per_subquery": 100,
                    "max_subqueries": 4,
                },
            },
            "expected": {
                "entity_types": ["entity-type:kg-entity"],
                "relationship_types": [
                    {
                        "semantic_id": "relationship-type:related-to",
                        "requirement": "required",
                        "direction": "source_to_target",
                    }
                ],
                "answer_concepts": [
                    "entity",
                    "relationship",
                    "evidence",
                ],
                "evidence_required": True,
                "temporal_required": False,
            },
            "routes": {
                "direct_graph": "required",
                "search": "required",
                "knowledge_base": "optional",
                "data_agent_ui": "optional",
                "data_agent_mcp": "required",
                "foundry_agent": "not_expected",
                "composed": "required",
            },
            "probes": {
                "direct_graph": {
                    "query": (
                        "MATCH (a:`KGEntity`)-[r:`related_to`]->(b:`KGEntity`) "
                        "RETURN a.entity_id AS source_id, "
                        "b.entity_id AS target_id, "
                        "r.evidence_id AS evidence_id LIMIT 10"
                    ),
                    "entity_bindings": [
                        {
                            "column": "source_id",
                            "semantic_id": "entity-type:kg-entity",
                        },
                        {
                            "column": "target_id",
                            "semantic_id": "entity-type:kg-entity",
                        },
                    ],
                    "relationship_bindings": [
                        {
                            "semantic_id": "relationship-type:related-to",
                            "source_column": "source_id",
                            "target_column": "target_id",
                            "direction": "source_to_target",
                            "evidence_column": "evidence_id",
                        }
                    ],
                    "canonical_id_columns": ["source_id", "target_id"],
                    "lineage_columns": ["evidence_id"],
                },
                "search": {
                    "top": 10,
                    "select_fields": [
                        "chunk_id",
                        "entity_ids",
                        "evidence_ids",
                        "asset_version_id",
                        "source_file_id",
                        "blob_url",
                        "source_locator_json",
                    ],
                    "vector_fields": ["chunk_vector"],
                    "semantic_configuration": "kg-chunks-semantic",
                    "canonical_id_fields": ["entity_ids"],
                    "citation_id_field": "chunk_id",
                    "asset_version_id_field": "asset_version_id",
                    "source_file_id_field": "source_file_id",
                    "blob_url_field": "blob_url",
                    "source_locator_field": "source_locator_json",
                    "evidence_id_field": "evidence_ids",
                },
                "data_agent_mcp": {
                    "tool_name": None,
                    "question_argument": "userQuestion",
                    "static_arguments": {},
                },
            },
        }
    ],
}

_GENERIC_RUNTIME_CONFIG: dict = {
    "schema_version": "1.0",
    "environment": "dev",
    "contract_hash": "REPLACE_WITH_COMPILED_CONTRACT_HASH",
    "deployment": {
        "artifact_validation_status": "pending",
        "knowledge_http_status": 200,
        "partial_source": False,
        "data_agent_published": False,
        "compiled_instruction_hash": "REPLACE_WITH_COMPILED_HASH",
        "deployed_instruction_hash": "REPLACE_WITH_DEPLOYED_HASH",
        "unintended_duplicate_deployments": 0,
        "breaking_change": False,
        "migration_approved": False,
        "receipt_path": (
            "../build/runs/REPLACE_WITH_RUN_ID/release/"
            "deployment-receipt.json"
        ),
    },
    "graph": {
        "workspace_id": "REPLACE_WITH_WORKSPACE_ID",
        "graph_model_id": "REPLACE_WITH_GRAPH_MODEL_ID",
    },
    "search": {
        "endpoint": "https://REPLACE.search.windows.net",
        "mode": "direct_search",
        "index_name": "REPLACE_WITH_INDEX_NAME",
        "embedding_endpoint": None,
        "embedding_deployment": "text-embedding-3-large",
        "embedding_dimensions": 1536,
        "api_version": "2024-07-01",
        "token_scope": "https://search.azure.com/.default",
        "obo_token_scope": None,
    },
    "data_agent_mcp": {
        "endpoint": (
            "https://api.fabric.microsoft.com/v1/mcp/workspaces/"
            "REPLACE_WITH_WORKSPACE_ID/dataagents/"
            "REPLACE_WITH_DATA_AGENT_ID/agent"
        ),
        "workspace_id": "REPLACE_WITH_WORKSPACE_ID",
        "data_agent_id": "REPLACE_WITH_DATA_AGENT_ID",
        "token_scope": "https://api.fabric.microsoft.com/.default",
        "protocol_version": "2025-03-26",
        "max_attempts": 3,
        "retry_base_delay_seconds": 0.25,
        "retry_jitter_seconds": 0.25,
        "request_timeout_seconds": 120,
    },
}

_GENERIC_ONTOLOGY_MODEL: dict = {
    "ontology": {
        "name": "FabricKG",
        "description": (
            "Fabric Knowledge Graph — domain-neutral starter model. "
            "Replace KGEntity with domain-specific types after "
            "'fabric-kg domain approve'."
        ),
        "version": "1.0.0",
        "modules": [
            {
                "name": "domain",
                "description": "Domain entities — customize for your use case",
                "entityTypeNames": ["KGEntity"],
                "relationshipTypeNames": ["related_to", "evidenced_by", "shown_in"],
            },
            {
                "name": "document-evidence",
                "description": "Document chunks extracted from source documents",
                "entityTypeNames": ["DocumentChunk"],
                "relationshipTypeNames": [],
            },
            {
                "name": "visual-evidence",
                "description": "Visual assets and figures for grounding",
                "entityTypeNames": ["Figure", "ImageAsset"],
                "relationshipTypeNames": [],
            },
            {
                "name": "retrieval",
                "description": "AI Search index records for graph-to-search bridge",
                "entityTypeNames": ["SearchIndexRecord"],
                "relationshipTypeNames": ["indexed_as"],
            },
        ],
        "entityTypes": [
            {
                "name": "KGEntity",
                "description": (
                    "Generic domain entity placeholder. Replace with domain-specific "
                    "types after 'fabric-kg domain approve'."
                ),
                "module": "domain",
                "properties": [
                    {"name": "display_name",  "type": "string",   "required": True},
                    {"name": "entity_id",     "type": "string",   "required": True},
                    {"name": "canonical_key", "type": "string",   "required": True},
                    {"name": "search_aliases","type": "string",   "required": False},
                    {"name": "entity_type",   "type": "string",   "required": False},
                    {"name": "description",   "type": "string",   "required": False},
                ],
                "dataBinding": {
                    "table": "entities",
                    "entityIdColumn": "entity_id",
                    "displayNameColumn": "display_name",
                    "typeFilterColumn": "entity_type",
                    "typeFilterValue": "KGEntity",
                    "additionalColumns": [
                        {"property": "canonical_key",  "column": "canonical_key"},
                        {"property": "search_aliases", "column": "search_aliases"},
                    ],
                },
            },
            {
                "name": "DocumentChunk",
                "description": "A chunk of source document content for bridge traversal.",
                "module": "document-evidence",
                "properties": [
                    {"name": "display_name",        "type": "string", "required": True},
                    {"name": "entity_id",           "type": "string", "required": True},
                    {"name": "chunk_id",            "type": "string", "required": True},
                    {"name": "related_entity_ids",  "type": "string", "required": False},
                    {"name": "entity_search_keys",  "type": "string", "required": False},
                    {"name": "content",             "type": "string", "required": False},
                ],
                "dataBinding": {
                    "table": "chunks",
                    "entityIdColumn": "chunk_id",
                    "displayNameColumn": "content",
                    "additionalColumns": [
                        {"property": "chunk_id",           "column": "chunk_id"},
                        {"property": "related_entity_ids", "column": "related_entity_ids"},
                        {"property": "entity_search_keys", "column": "entity_search_keys"},
                    ],
                },
            },
            {
                "name": "Figure",
                "description": "A figure or diagram extracted from a source document.",
                "module": "visual-evidence",
                "properties": [
                    {"name": "display_name", "type": "string",   "required": True},
                    {"name": "blob_url",     "type": "blob_url", "required": True},
                ],
                "dataBinding": {
                    "table": "document_elements",
                    "entityIdColumn": "document_element_id",
                    "displayNameColumn": "title",
                    "additionalColumns": [
                        {"property": "blob_url", "column": "blob_url"},
                    ],
                },
            },
            {
                "name": "ImageAsset",
                "description": "A visual asset stored in blob storage.",
                "module": "visual-evidence",
                "properties": [
                    {"name": "display_name", "type": "string",   "required": True},
                    {"name": "entity_id",    "type": "string",   "required": True},
                    {"name": "blob_url",     "type": "blob_url", "required": True},
                ],
                "dataBinding": {
                    "table": "visual_assets",
                    "entityIdColumn": "image_id",
                    "displayNameColumn": "caption",
                    "additionalColumns": [
                        {"property": "blob_url",  "column": "blob_url"},
                        {"property": "entity_id", "column": "image_id"},
                    ],
                },
            },
            {
                "name": "SearchIndexRecord",
                "description": "AI Search index record — target of the indexed_as bridge relationship.",
                "module": "retrieval",
                "properties": [
                    {"name": "display_name",    "type": "string", "required": True},
                    {"name": "entity_id",       "type": "string", "required": True},
                    {"name": "search_record_id","type": "string", "required": True},
                ],
                "dataBinding": {
                    "table": "chunks",
                    "entityIdColumn": "chunk_id",
                    "displayNameColumn": "chunk_id",
                    "additionalColumns": [
                        {"property": "search_record_id", "column": "chunk_id"},
                    ],
                },
            },
        ],
        "relationshipTypes": [
            {
                "name": "related_to",
                "description": "Generic relationship between domain entities.",
                "module": "domain",
                "sourceType": "KGEntity",
                "targetType": "KGEntity",
                "inversePolicy": "none",
                "dataBinding": {
                    "table": "relationships",
                    "relationshipIdColumn": "relationship_id",
                    "sourceEntityIdColumn": "source_entity_id",
                    "targetEntityIdColumn": "target_entity_id",
                    "typeFilterColumn": "relationship_type",
                    "typeFilterValue": "related_to",
                },
            },
            {
                "name": "evidenced_by",
                "description": "Links a domain entity to the document chunk providing evidence.",
                "module": "domain",
                "sourceType": "KGEntity",
                "targetType": "DocumentChunk",
                "inversePolicy": "none",
                "dataBinding": {
                    "table": "relationships",
                    "relationshipIdColumn": "relationship_id",
                    "sourceEntityIdColumn": "source_entity_id",
                    "targetEntityIdColumn": "target_entity_id",
                    "typeFilterColumn": "relationship_type",
                    "typeFilterValue": "evidenced_by",
                },
            },
            {
                "name": "shown_in",
                "description": "Links a domain entity to a visual figure it appears in.",
                "module": "domain",
                "sourceType": "KGEntity",
                "targetType": "Figure",
                "inversePolicy": "none",
                "dataBinding": {
                    "table": "relationships",
                    "relationshipIdColumn": "relationship_id",
                    "sourceEntityIdColumn": "source_entity_id",
                    "targetEntityIdColumn": "target_entity_id",
                    "typeFilterColumn": "relationship_type",
                    "typeFilterValue": "shown_in",
                },
            },
            {
                "name": "indexed_as",
                "description": "Links a DocumentChunk to its SearchIndexRecord.",
                "module": "retrieval",
                "sourceType": "DocumentChunk",
                "targetType": "SearchIndexRecord",
                "inversePolicy": "none",
                "dataBinding": {
                    "table": "relationships",
                    "relationshipIdColumn": "relationship_id",
                    "sourceEntityIdColumn": "source_entity_id",
                    "targetEntityIdColumn": "target_entity_id",
                    "typeFilterColumn": "relationship_type",
                    "typeFilterValue": "indexed_as",
                },
            },
        ],
    }
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _FileOutcome(NamedTuple):
    path: Path
    status: str       # "created" | "skipped" | "error"
    error: str | None = None


def _write_file(target: Path, content: str, *, force: bool) -> _FileOutcome:
    """Write *content* to *target*; skip if exists and *force* is False."""
    if target.exists() and not force:
        return _FileOutcome(target, "skipped")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return _FileOutcome(target, "created")
    except OSError as exc:
        return _FileOutcome(target, "error", str(exc))


def _ensure_dir(target: Path) -> _FileOutcome:
    """Create *target* directory tree; treat already-existing as created (idempotent)."""
    try:
        existed = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        return _FileOutcome(target, "skipped" if existed else "created")
    except OSError as exc:
        return _FileOutcome(target, "error", str(exc))


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("init", epilog=_INIT_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--target", "target_dir", default=".", show_default=True,
              type=click.Path(),
              help="Target directory to scaffold. Defaults to the current directory.")
@click.option("--template", default="default", show_default=True,
              type=click.Choice(["default", "csv-only"]),
              help="Project template: 'default' includes all source types; "
                   "'csv-only' limits source hints to CSV ingestion.")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite existing files. Without --force existing files are skipped.")
def init_cmd(target_dir: str, template: str, force: bool) -> None:
    """Scaffold a new domain-neutral project in the target directory.

    Creates:
      fabric-kg.yaml          (GPT-4.1 + text-embedding-3-large config)
      domain.yaml             (schema-valid draft domain contract)
      .env.example            (only when absent — never overwritten)
      infra/environments/{dev,test,prod}.yaml
      ontology/contract.yaml  (draft canonical semantic authority)
      ontology/mappings.yaml  (semantic-to-physical bindings)
      ontology/vocabulary.yaml
      ontology/ids.lock.json  (semantic and preserved Fabric IDs)
      ontology/model.yaml     (legacy-compatible generated-input scaffold)
      ontology/environments/{dev,test,prod}.json
      evaluation/competency.yaml
      evaluation/runtime-config.example.json
      build/{enriched,parquet,semantic,ontology,graph,search,agents}/  dist/

    Exit codes: 0 success · 1 error · 2 already initialized (no-op).
    """
    root = Path(target_dir).resolve()

    # ── Exit 2: already initialized ──────────────────────────────────────────
    marker = root / "fabric-kg.yaml"
    if marker.exists() and not force:
        click.echo(
            f"[init] already initialized: {marker}\n"
            "[init] use --force to reinitialize or fill in missing files.",
            err=True,
        )
        sys.exit(2)

    outcomes: list[_FileOutcome] = []

    # 1. fabric-kg.yaml
    fg_content = _FABRIC_KG_YAML_CSV_ONLY if template == "csv-only" else _FABRIC_KG_YAML
    outcomes.append(_write_file(root / "fabric-kg.yaml", fg_content, force=force))

    # 2. domain.yaml — schema-valid draft via domain service
    try:
        contract = default_domain_contract()
        domain_yaml_text = render_domain_contract_yaml(contract)
    except Exception as exc:  # noqa: BLE001
        domain_yaml_text = f"# TODO: fill in domain.yaml — auto-generation failed: {exc}\n"
    outcomes.append(_write_file(root / "domain.yaml", domain_yaml_text, force=force))

    # 3. .env.example — write only when absent (never overwritten, even with --force)
    env_example = root / ".env.example"
    if not env_example.exists():
        outcomes.append(_write_file(env_example, _ENV_EXAMPLE, force=True))
    else:
        outcomes.append(_FileOutcome(env_example, "skipped"))

    # 4. infra/environments/{dev,test,prod}.yaml
    for env in ("dev", "test", "prod"):
        outcomes.append(_write_file(
            root / "infra" / "environments" / f"{env}.yaml",
            _infra_env_yaml(env),
            force=force,
        ))

    # 5. Canonical semantic authority — draft until reviewed and approved.
    outcomes.append(_write_file(
        root / "ontology" / "contract.yaml",
        yaml.safe_dump(
            _GENERIC_SEMANTIC_CONTRACT,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
        force=force,
    ))
    outcomes.append(_write_file(
        root / "ontology" / "mappings.yaml",
        yaml.safe_dump(
            _GENERIC_SEMANTIC_MAPPINGS,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
        force=force,
    ))
    outcomes.append(_write_file(
        root / "ontology" / "vocabulary.yaml",
        yaml.safe_dump(
            _GENERIC_SEMANTIC_VOCABULARY,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
        force=force,
    ))

    # 6. ontology/model.yaml — compatibility scaffold for compile-ontology
    model_yaml_text = yaml.safe_dump(
        _GENERIC_ONTOLOGY_MODEL,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    outcomes.append(_write_file(root / "ontology" / "model.yaml", model_yaml_text, force=force))

    # 7. ontology/ids.lock.json — shared semantic/Fabric stable ID lock
    outcomes.append(_write_file(
        root / "ontology" / "ids.lock.json",
        json.dumps(_GENERIC_IDS_LOCK, indent=2),
        force=force,
    ))

    # 8. ontology/environments/{dev,test,prod}.json
    for env in ("dev", "test", "prod"):
        outcomes.append(_write_file(
            root / "ontology" / "environments" / f"{env}.json",
            _ontology_env_json(env),
            force=force,
        ))

    # 9. Route-aware runtime acceptance scaffolds.
    outcomes.append(
        _write_file(
            root / "evaluation" / "competency.yaml",
            yaml.safe_dump(
                _GENERIC_COMPETENCY_SUITE,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
            force=force,
        )
    )
    outcomes.append(
        _write_file(
            root / "evaluation" / "runtime-config.example.json",
            json.dumps(_GENERIC_RUNTIME_CONFIG, indent=2),
            force=force,
        )
    )

    # 10. Build and dist directories
    for sub in (
        "build/enriched",
        "build/parquet",
        "build/semantic",
        "build/ontology",
        "build/graph",
        "build/search",
        "build/agents",
        "build/validation",
        "build/deployment",
        "build/evaluation",
        "dist",
    ):
        outcomes.append(_ensure_dir(root / sub))

    # ── Report ───────────────────────────────────────────────────────────────
    for o in outcomes:
        rel = _rel_path(o.path, root)
        if o.status == "created":
            click.echo(f"[init]   created : {rel}")
        elif o.status == "skipped":
            click.echo(f"[init]   skipped : {rel}  (already exists)")
        else:
            click.echo(f"[init]   ERROR   : {rel}  — {o.error}", err=True)

    created = [o for o in outcomes if o.status == "created"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    errors  = [o for o in outcomes if o.status == "error"]

    click.echo("")
    click.echo(f"[init] {len(created)} created, {len(skipped)} skipped, {len(errors)} error(s)")

    if errors:
        click.echo("[init] FAILED: partial initialization due to I/O errors above.", err=True)
        sys.exit(1)

    click.echo("[init] Scaffold complete.")
    click.echo("[init] Next steps:")
    click.echo("  1. Edit domain.yaml with your domain details")
    click.echo("  2. fabric-kg domain validate")
    click.echo("  3. fabric-kg domain review")
    click.echo("  4. fabric-kg domain approve")
    click.echo("  5. Edit ontology/contract.yaml, mappings.yaml, and vocabulary.yaml")
    click.echo("  6. fabric-kg inspect-ontology")
    click.echo("  7. Approve the semantic contract before compilation")
    click.echo("  8. Replace evaluation/competency.yaml with typed business questions")


def _rel_path(p: Path, root: Path) -> str:
    """Return relative path string or absolute if outside root."""
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)
