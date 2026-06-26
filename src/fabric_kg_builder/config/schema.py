"""Pydantic configuration models for fabric-kg-builder.

Covers all typed config sections: foundry, fabric, blob_storage,
ai_search, document_intelligence, auth.  Secrets are NEVER stored here —
they live in .env and are resolved via environment variables.
"""

from typing import Optional
from pydantic import BaseModel, Field


class FoundryConfig(BaseModel):
    """Azure AI Foundry — LLM + embedding deployment refs (non-secret)."""

    endpoint: str = Field(description="Foundry project endpoint URL (from ${AZURE_AI_FOUNDRY_ENDPOINT})")
    openai_endpoint: str = Field(
        default="",
        description="Azure OpenAI endpoint (e.g. https://<name>.openai.azure.com/). "
        "Required for live SDK calls. Source: ${AZURE_OPENAI_ENDPOINT} or foundry.openai_endpoint.",
    )
    project: str = Field(default="example-project", description="Foundry project name")
    chat_deployment: str = Field(default="gpt-5-4-mini")
    embedding_deployment: str = Field(default="embedding")
    embedding_dimensions: int = Field(default=1536, description="Must match AI Search chunk_vector field width")
    api_version: str = Field(
        default="2024-12-01-preview",
        description="Azure OpenAI REST API version forwarded to AzureOpenAI client.",
    )


class FabricConfig(BaseModel):
    """Microsoft Fabric Lakehouse targeting."""

    workspace_id: str = Field(description="Fabric workspace GUID")
    lakehouse_item_id: str = Field(description="Lakehouse item GUID within the workspace")
    schema_name: str = Field(
        default="dbo",
        description="Schema name in the schema-enabled Lakehouse (Tables/{schema}/{table}).",
    )


class BlobStorageConfig(BaseModel):
    """Azure Blob Storage for visual assets."""

    account_name: str = Field(default="")
    container: str = Field(default="kg-assets")
    path_prefix: str = Field(default="")


class AiSearchConfig(BaseModel):
    """Azure AI Search — optional; skipped when enabled=False."""

    enabled: bool = Field(default=False)
    service_name: Optional[str] = None
    endpoint: str = Field(
        default="",
        description="Full service endpoint URL (e.g. https://example-search.search.windows.net).",
    )
    index_prefix: str = Field(default="kg-")


class DocumentIntelligenceConfig(BaseModel):
    """Azure AI Document Intelligence — required for PDF/image extraction."""

    endpoint: str = Field(default="", description="From ${AZURE_DOCINTEL_ENDPOINT}")


class Config(BaseModel):
    """Merged runtime configuration for one environment."""

    env: str = Field(default="dev")
    foundry: FoundryConfig
    fabric: FabricConfig
    blob: BlobStorageConfig
    ai_search: AiSearchConfig
    document_intelligence: DocumentIntelligenceConfig
    auth_strategy: str = Field(default="DefaultAzureCredential")
