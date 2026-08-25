"""Azure AI Search indexer/skillset contracts for integrated vectorization."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fabric_kg_builder.sources.chunker import TiktokenTokenizer

from .embedding_input import EMBEDDING_TOKEN_ENCODING, document_embedding_text


DEFAULT_API_VERSION = "2025-09-01"


@dataclass(frozen=True)
class IntegratedVectorizationConfig:
    """Non-secret settings for a Blob JSON indexer and Azure OpenAI skill."""

    source_container: str
    source_path: str
    storage_resource_id: str
    azure_openai_endpoint: str
    azure_openai_deployment: str
    storage_account_url: str = ""
    azure_openai_model: str = "text-embedding-3-large"
    dimensions: int = 1536
    data_source_name: str = ""
    skillset_name: str = ""
    indexer_name: str = ""
    api_version: str = DEFAULT_API_VERSION
    poll_interval_seconds: float = 2.0
    poll_timeout_seconds: float = 300.0


@dataclass
class IndexerRunResult:
    """Terminal result of an indexer run."""

    indexer_name: str
    status: str
    succeeded: bool
    error_message: str | None = None
    raw_status: dict[str, Any] = field(default_factory=dict)


class IndexerRunError(RuntimeError):
    """Raised when the indexer reaches a failed terminal state."""


@dataclass(frozen=True)
class SourceStageResult:
    """A deterministic Blob source document upload result."""

    container: str
    blob_name: str
    byte_count: int


def prepare_source_documents(
    docs: list[dict[str, Any]],
    *,
    vector_fields: set[str],
    id_field: str,
) -> list[dict[str, Any]]:
    """Return deterministic, lineage-preserving JSON source documents.

    The indexer owns vector creation, so every declared vector field is
    excluded even if a prior direct-embedding build populated it.
    """
    prepared = [
        {key: value for key, value in doc.items() if key not in vector_fields}
        for doc in docs
    ]
    tokenizer = TiktokenTokenizer(EMBEDDING_TOKEN_ENCODING)
    for doc in prepared:
        record_id = str(doc.get(id_field, ""))
        doc[id_field] = base64.urlsafe_b64encode(
            record_id.encode("utf-8")
        ).decode("ascii").rstrip("=")
        doc["embedding_text"] = document_embedding_text(
            doc,
            text_field="embedding_text",
            tokenizer=tokenizer,
        )
    return sorted(prepared, key=lambda doc: str(doc.get(id_field, "")))


def stage_source_documents(
    source_file: Any,
    *,
    config: IntegratedVectorizationConfig,
    vector_fields: set[str],
    blob_service_client: Any = None,
    prepared: bool = False,
) -> SourceStageResult:
    """Upload compiled JSON source documents to the configured Blob path.

    The path comes from environment configuration and is therefore stable
    across retries.  Overwrite is intentional: a build's deterministic source
    representation is the authoritative input for its next indexer run.
    """
    from pathlib import Path

    path = Path(source_file)
    if not path.is_file():
        raise FileNotFoundError(
            f"Integrated-vectorization source file is missing: {path}. "
            "Run 'fabric-kg compile-search' before deployment."
        )
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Source documents are not valid JSON: {path}") from exc
    if not isinstance(records, list):
        raise ValueError(f"Source documents must be a JSON array: {path}")
    if any(not isinstance(record, dict) for record in records):
        raise ValueError(f"Source documents must be JSON objects: {path}")
    if any(
        vector_fields.intersection(record)
        for record in records
    ):
        raise ValueError(
            f"Source documents must not contain vector field(s) "
            f"{', '.join(sorted(vector_fields))}: {path}"
        )
    if not prepared:
        records = prepare_source_documents(
            records,
            vector_fields=vector_fields,
            id_field="chunk_id",
        )

    blob_name = config.source_path.strip("/")
    if not blob_name:
        raise ValueError("integrated_vectorization.source_path must not be empty.")
    if blob_service_client is None:
        from fabric_kg_builder.azure_identity import (
            default_azure_credential,
        )

        try:
            from azure.identity import DefaultAzureCredential  # type: ignore[import]
            from azure.storage.blob import BlobServiceClient  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "azure-identity and azure-storage-blob are required to stage "
                "integrated-vectorization source documents."
            ) from exc
        account_url = config.storage_account_url
        if not account_url:
            resource_id = config.storage_resource_id.rstrip("/")
            if "/storageaccounts/" not in resource_id.lower():
                raise ValueError(
                    "storage_resource_id must identify a Microsoft.Storage/"
                    "storageAccounts resource when storage_account_url is unset."
                )
            account_url = f"https://{resource_id.rsplit('/', 1)[-1]}.blob.core.windows.net"
        blob_service_client = BlobServiceClient(
            account_url=account_url,
            credential=default_azure_credential(),
        )

    payload = json.dumps(
        records, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    blob_service_client.get_blob_client(
        container=config.source_container,
        blob=blob_name,
    ).upload_blob(payload, overwrite=True)
    return SourceStageResult(
        container=config.source_container,
        blob_name=blob_name,
        byte_count=len(payload),
    )


def build_data_source(
    *, name: str, config: IntegratedVectorizationConfig
) -> dict[str, Any]:
    """Build an Azure Blob data source using the Search system identity."""
    return {
        "name": name,
        "type": "azureblob",
        "credentials": {
            "connectionString": f"ResourceId={config.storage_resource_id};",
        },
        "container": {
            "name": config.source_container,
            "query": config.source_path,
        },
    }


def build_skillset(
    *, name: str, config: IntegratedVectorizationConfig, vector_field: str
) -> dict[str, Any]:
    """Build an AzureOpenAIEmbeddingSkill using the system-assigned identity.

    ``authIdentity`` is intentionally omitted: Azure AI Search interprets that
    as its system-assigned managed identity.
    """
    return {
        "name": name,
        "description": "Generate chunk vectors from staged embedding text.",
        "skills": [
            {
                "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
                "name": "azure-openai-embedding",
                "description": "Embed each staged document with Azure OpenAI.",
                "context": "/document",
                "resourceUri": config.azure_openai_endpoint,
                "deploymentId": config.azure_openai_deployment,
                "modelName": config.azure_openai_model,
                "dimensions": config.dimensions,
                "inputs": [{"name": "text", "source": "/document/embedding_text"}],
                "outputs": [{"name": "embedding", "targetName": vector_field}],
            }
        ],
    }


def build_indexer(
    *,
    name: str,
    index_name: str,
    data_source_name: str,
    skillset_name: str,
    schema: dict[str, Any],
    vector_field: str,
) -> dict[str, Any]:
    """Build a JSON-array Blob indexer preserving every non-vector index field."""
    source_fields = [
        item["name"]
        for item in schema.get("fields", [])
        if item.get("name") != vector_field
    ]
    return {
        "name": name,
        "dataSourceName": data_source_name,
        "targetIndexName": index_name,
        "skillsetName": skillset_name,
        "parameters": {
            "configuration": {
                "parsingMode": "jsonArray",
                "dataToExtract": "contentAndMetadata",
            }
        },
        "fieldMappings": [
            {"sourceFieldName": field_name, "targetFieldName": field_name}
            for field_name in source_fields
        ],
        "outputFieldMappings": [
            {
                "sourceFieldName": f"/document/{vector_field}/*",
                "targetFieldName": vector_field,
            }
        ],
    }


def _names(index_name: str, config: IntegratedVectorizationConfig) -> tuple[str, str, str]:
    safe = index_name.replace("_", "-")
    return (
        config.data_source_name or f"{safe}-source",
        config.skillset_name or f"{safe}-embeddings",
        config.indexer_name or f"{safe}-indexer",
    )


def deploy_integrated_vectorization(
    *,
    endpoint: str,
    index_name: str,
    schema: dict[str, Any],
    vector_field: str,
    config: IntegratedVectorizationConfig,
    token_provider: Callable[[], str],
    requests_module: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> IndexerRunResult:
    """Create data source, skillset and indexer, then run and poll the indexer."""
    if requests_module is None:
        import requests as requests_module  # type: ignore[import]

    data_source_name, skillset_name, indexer_name = _names(index_name, config)
    headers = {
        "Authorization": f"Bearer {token_provider()}",
        "Content-Type": "application/json",
    }
    root = endpoint.rstrip("/")
    api = config.api_version or DEFAULT_API_VERSION

    resources = (
        ("datasources", data_source_name, build_data_source(name=data_source_name, config=config)),
        ("skillsets", skillset_name, build_skillset(
            name=skillset_name, config=config, vector_field=vector_field
        )),
        ("indexers", indexer_name, build_indexer(
            name=indexer_name,
            index_name=index_name,
            data_source_name=data_source_name,
            skillset_name=skillset_name,
            schema=schema,
            vector_field=vector_field,
        )),
    )
    for resource, name, body in resources:
        response = requests_module.put(
            f"{root}/{resource}/{name}?api-version={api}",
            headers=headers,
            json=body,
            timeout=60,
        )
        response.raise_for_status()

    response = requests_module.post(
        f"{root}/indexers/{indexer_name}/reset?api-version={api}",
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()

    response = requests_module.post(
        f"{root}/indexers/{indexer_name}/run?api-version={api}",
        headers=headers,
        timeout=60,
    )
    if response.status_code != 409:
        response.raise_for_status()

    deadline = clock() + config.poll_timeout_seconds
    while True:
        response = requests_module.get(
            f"{root}/indexers/{indexer_name}/status?api-version={api}",
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        status = response.json()
        last = status.get("lastResult") or {}
        state = str(last.get("status") or status.get("status") or "").lower()
        error = last.get("errorMessage") or status.get("errorMessage")
        if state in {"success", "persistentfailure", "transientfailure"}:
            succeeded = state == "success"
            result = IndexerRunResult(indexer_name, state, succeeded, error, status)
            if not succeeded:
                raise IndexerRunError(
                    f"Indexer {indexer_name!r} failed ({state}): {error or 'no error message'}"
                )
            return result
        if clock() >= deadline:
            raise IndexerRunError(
                f"Indexer {indexer_name!r} did not finish within "
                f"{config.poll_timeout_seconds} seconds."
            )
        sleep(config.poll_interval_seconds)
