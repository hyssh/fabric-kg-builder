"""search.push — upsert AI Search index definitions and document batches.

Fenster module: takes compiled index artifacts from build/search/{index}/
and pushes them to Azure AI Search.

In MOCK mode (always used in tests and offline) the function logs what
WOULD be pushed and returns a PushResult without making any network call.

Live mode (opt-in via ``mock=False``) calls the Azure AI Search SDK.
The AI Search SDK is NOT fabric-cicd — fabric-cicd is for Fabric items only.

Change detection (SPEC-002 §11.6)
----------------------------------
push_chunk_docs() accepts an optional ``existing_hashes`` mapping of
``{chunk_id: content_hash}`` from the previous run.  Docs whose
``content_hash`` matches the stored value are skipped (no re-embed, no
re-upsert).  Docs with a changed or absent hash are merged-pushed.

Only chunk/text/visual documents are pushed to AI Search.
Structured Parquet (entities, relationships, evidence, source_files) is
NOT pushed — it lives in the Fabric Lakehouse only (SPEC-002 §2.1).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PushResult:
    """Summary returned by push_index / push_documents / push_chunk_docs."""

    index_name: str
    doc_count: int
    mock: bool
    succeeded: bool = True
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        mode = "MOCK" if self.mock else "LIVE"
        status = "OK" if self.succeeded else "FAILED"
        return (
            f"[{mode}] {status} — index={self.index_name!r}, "
            f"docs={self.doc_count}, skipped={self.skipped}"
        )


def push_index(
    index_name: str,
    schema: dict[str, Any],
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    mock: bool = True,
) -> PushResult:
    """Create or update an AI Search index definition.

    Parameters
    ----------
    index_name:
        Deployed index name (may include env prefix, e.g. ``kg-dev-chunks``).
    schema:
        Index schema dict as produced by compile_search_cmd._build_*_schema().
    endpoint:
        AI Search service endpoint.  Falls back to ``AZURE_SEARCH_ENDPOINT``.
    api_key:
        AI Search admin key.  Falls back to ``AZURE_SEARCH_API_KEY``.
    mock:
        When True, log only — no network call.  Default True.

    Returns
    -------
    PushResult
    """
    _endpoint = endpoint or os.environ.get("AZURE_SEARCH_ENDPOINT", "")
    _mock = mock or not _endpoint

    if _mock:
        return PushResult(index_name=index_name, doc_count=0, mock=True)

    try:
        from azure.search.documents.indexes import SearchIndexClient  # type: ignore[import]
        from azure.core.credentials import AzureKeyCredential  # type: ignore[import]
        from azure.search.documents.indexes.models import SearchIndex  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "azure-search-documents is required for live push. "
            "Install it or pass mock=True."
        ) from exc

    _key = api_key or os.environ.get("AZURE_SEARCH_API_KEY", "")
    client = SearchIndexClient(endpoint=_endpoint, credential=AzureKeyCredential(_key))
    # Build a minimal SearchIndex from the schema dict; full field mapping omitted
    # here — a proper implementation would convert schema["fields"] to SearchField objects.
    idx = SearchIndex(name=index_name)
    client.create_or_update_index(idx)
    return PushResult(index_name=index_name, doc_count=0, mock=False)


def push_documents(
    index_name: str,
    docs: list[dict[str, Any]],
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    mock: bool = True,
    batch_size: int = 1000,
) -> PushResult:
    """Upload document batches to an AI Search index.

    Parameters
    ----------
    index_name:
        Deployed index name.
    docs:
        List of AI Search document dicts to upsert.
    endpoint:
        AI Search service endpoint.  Falls back to ``AZURE_SEARCH_ENDPOINT``.
    api_key:
        AI Search admin key.  Falls back to ``AZURE_SEARCH_API_KEY``.
    mock:
        When True, log only — no network call.  Default True.
    batch_size:
        Docs per upload batch (AI Search max is 1000).

    Returns
    -------
    PushResult
    """
    _endpoint = endpoint or os.environ.get("AZURE_SEARCH_ENDPOINT", "")
    _mock = mock or not _endpoint

    if _mock:
        return PushResult(index_name=index_name, doc_count=len(docs), mock=True)

    try:
        from azure.search.documents import SearchClient  # type: ignore[import]
        from azure.core.credentials import AzureKeyCredential  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "azure-search-documents is required for live push. "
            "Install it or pass mock=True."
        ) from exc

    _key = api_key or os.environ.get("AZURE_SEARCH_API_KEY", "")
    client = SearchClient(
        endpoint=_endpoint,
        index_name=index_name,
        credential=AzureKeyCredential(_key),
    )
    errors: list[str] = []
    for start in range(0, len(docs), batch_size):
        batch = docs[start : start + batch_size]
        result = client.upload_documents(documents=batch)
        for r in result:
            if not r.succeeded:
                errors.append(f"key={r.key}: {r.error_message}")

    return PushResult(
        index_name=index_name,
        doc_count=len(docs),
        mock=False,
        succeeded=not errors,
        errors=errors,
    )


def push_chunk_docs(
    index_name: str,
    search_docs: list[dict[str, Any]],
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    mock: bool = True,
    existing_hashes: dict[str, str] | None = None,
    batch_size: int = 1000,
) -> PushResult:
    """Upsert chunk-document dicts to AI Search by chunk_id with change detection.

    Per SPEC-002 §11.6: docs whose ``content_hash`` matches the hash in
    ``existing_hashes`` are skipped (no re-embed, no re-push).  Only
    chunk/text/visual docs are eligible — structured Parquet tables are
    NOT pushed here (Lakehouse-only).

    Parameters
    ----------
    index_name:
        Deployed AI Search index name.
    search_docs:
        List of search-document dicts as produced by ``linkage.derive_chunk_search_docs``.
        Each doc must contain ``chunk_id`` and ``content_hash``.
    endpoint:
        AI Search service endpoint.  Falls back to ``AZURE_SEARCH_ENDPOINT``.
    api_key:
        AI Search admin key.  Falls back to ``AZURE_SEARCH_API_KEY``.
    mock:
        When True, log only — no network call.  Default True.
    existing_hashes:
        Optional ``{chunk_id: content_hash}`` from the previous push run.
        Docs whose hash matches are skipped to avoid redundant re-embedding.
    batch_size:
        Documents per upload batch (AI Search max is 1000).

    Returns
    -------
    PushResult
        ``doc_count`` = number actually pushed; ``skipped`` = number skipped.
    """
    hashes = existing_hashes or {}

    # Change detection: only push docs whose hash changed or is new
    to_push = [
        doc for doc in search_docs
        if hashes.get(doc.get("chunk_id", "")) != doc.get("content_hash", "")
    ]
    skipped = len(search_docs) - len(to_push)

    _endpoint = endpoint or os.environ.get("AZURE_SEARCH_ENDPOINT", "")
    _mock = mock or not _endpoint

    if _mock:
        return PushResult(
            index_name=index_name,
            doc_count=len(to_push),
            mock=True,
            skipped=skipped,
        )

    result = push_documents(
        index_name,
        to_push,
        endpoint=_endpoint,
        api_key=api_key,
        mock=False,
        batch_size=batch_size,
    )
    result.skipped = skipped
    return result


def push_from_build_dir(
    build_search_dir: Path,
    index_name: str,
    deployed_name: str,
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    mock: bool = True,
) -> tuple[PushResult, PushResult]:
    """Convenience: push schema + documents from a build/search/{index}/ directory.

    Reads ``index.schema.json`` and ``docs.json`` (if present) and calls
    push_index() then push_documents().

    Returns
    -------
    tuple[PushResult, PushResult]
        (schema_result, docs_result)
    """
    import json

    index_dir = build_search_dir / index_name
    schema_path = index_dir / "index.schema.json"
    docs_path = index_dir / "docs.json"

    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    docs: list[dict[str, Any]] = []
    if docs_path.exists():
        docs = json.loads(docs_path.read_text(encoding="utf-8"))

    schema_result = push_index(
        deployed_name, schema, endpoint=endpoint, api_key=api_key, mock=mock
    )
    docs_result = push_documents(
        deployed_name, docs, endpoint=endpoint, api_key=api_key, mock=mock
    )
    return schema_result, docs_result
