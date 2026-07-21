"""search.embeddings — attach embedding vectors to AI Search documents.

Verbal module: takes a batch of docs and a text field name, calls the
embedding model, and returns the docs with a vector field attached.

Offline / test mode: if ``mock=True`` (or no Azure OpenAI endpoint is set)
the function fills each vector with zeros at the declared dimension — no
network call is made.  Tests always pass ``mock=True``.

Live mode calls an Azure OpenAI-compatible Foundry model endpoint through
``openai.AzureOpenAI``.
"""

from __future__ import annotations

import os
from typing import Any

from fabric_kg_builder.sources.chunker import TiktokenTokenizer

from .embedding_input import EMBEDDING_TOKEN_ENCODING, document_embedding_text

_VECTOR_DIMS = 1536  # LOCKED — text-embedding-3-large (SPEC-002 §11.7)
_DEFAULT_BATCH_SIZE = 256
_MAX_BATCH_SIZE = 2048


def _attach_live_vectors(
    docs: list[dict[str, Any]],
    *,
    text_field: str,
    vector_field: str,
    dimensions: int,
    batch_size: int,
    create_embeddings: Any,
) -> list[dict[str, Any]]:
    """Attach live embeddings in provider-safe, order-preserving batches."""
    tokenizer = TiktokenTokenizer(EMBEDDING_TOKEN_ENCODING)
    for batch_start in range(0, len(docs), batch_size):
        batch_docs = docs[batch_start : batch_start + batch_size]
        response = create_embeddings(
            [
                document_embedding_text(
                    doc,
                    text_field=text_field,
                    tokenizer=tokenizer,
                )
                for doc in batch_docs
            ]
        )
        response_items = list(response.data)
        if len(response_items) != len(batch_docs):
            raise ValueError(
                "Embedding response count does not match the request batch: "
                f"requested={len(batch_docs)}, returned={len(response_items)}."
            )
        if all(isinstance(getattr(item, "index", None), int) for item in response_items):
            response_items.sort(key=lambda item: item.index)
        for doc, emb_item in zip(batch_docs, response_items):
            vector = list(emb_item.embedding)
            if len(vector) != dimensions:
                raise ValueError(
                    "Embedding dimensions do not match the configured index: "
                    f"expected={dimensions}, returned={len(vector)}."
                )
            doc[vector_field] = vector
    return docs


def attach_vectors(
    docs: list[dict[str, Any]],
    text_field: str,
    vector_field: str,
    *,
    mock: bool = False,
    endpoint: str | None = None,
    deployment: str = "embedding",
    dimensions: int = _VECTOR_DIMS,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """Attach ``vector_field`` embeddings to each doc in-place.

    Parameters
    ----------
    docs:
        List of AI Search document dicts (from linkage.derive_*).
    text_field:
        Name of the string field to embed (e.g. ``"embedding_text"``).
    vector_field:
        Name of the vector field to write (e.g. ``"chunk_vector"``).
    mock:
        When True, fill with zero vectors — no network call.
        Auto-enabled when neither ``AZURE_OPENAI_ENDPOINT`` nor the legacy
        ``AZURE_AI_FOUNDRY_ENDPOINT`` variable is set.
    endpoint:
        Azure OpenAI endpoint (https://<name>.openai.azure.com/).
        Falls back to ``AZURE_AI_FOUNDRY_ENDPOINT`` for backward compat.
    deployment:
        Foundry embedding deployment name.
    dimensions:
        Vector dimensions. LOCKED at 1536.
    batch_size:
        Maximum documents per provider request. Azure OpenAI accepts at most
        2048 inputs; the lower default limits payload size and rate spikes.

    Returns
    -------
    list[dict]
        The same docs list (mutated in-place) with ``vector_field`` attached.
    """
    if not docs:
        return docs
    if not 1 <= batch_size <= _MAX_BATCH_SIZE:
        raise ValueError(
            f"batch_size must be between 1 and {_MAX_BATCH_SIZE}; got {batch_size}."
        )

    _endpoint = (
        endpoint
        or os.environ.get("AZURE_OPENAI_ENDPOINT")
        or os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT", "")
    )
    _use_mock = mock or not _endpoint

    if _use_mock:
        zero_vec = [0.0] * dimensions
        for doc in docs:
            doc[vector_field] = zero_vec
        return docs

    # Live path — lazy import to avoid hard deps in offline environments.
    if ".services.ai.azure.com" in _endpoint:
        try:
            from azure.ai.projects import AIProjectClient  # type: ignore[import]
            from azure.identity import DefaultAzureCredential  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "azure-ai-projects and azure-identity are required for "
                "Foundry project embeddings."
            ) from exc
        project_endpoint = (
            os.environ.get("AZURE_AI_PROJECT_ENDPOINT") or _endpoint
        )
        project = AIProjectClient(
            endpoint=project_endpoint,
            credential=DefaultAzureCredential(),
        )
        aoai = project.get_openai_client()
        return _attach_live_vectors(
            docs,
            text_field=text_field,
            vector_field=vector_field,
            dimensions=dimensions,
            batch_size=batch_size,
            create_embeddings=lambda texts: aoai.embeddings.create(
                model=deployment,
                input=texts,
                dimensions=dimensions,
            ),
        )

    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # type: ignore[import]
        from openai import AzureOpenAI  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "openai and azure-identity are required for live embeddings. "
            "Install them or pass mock=True."
        ) from exc

    api_key = os.environ.get("AZURE_AI_FOUNDRY_API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY")
    if api_key:
        aoai = AzureOpenAI(
            azure_endpoint=_endpoint,
            api_key=api_key,
            api_version="2024-12-01-preview",
        )
    else:
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://cognitiveservices.azure.com/.default",
        )
        aoai = AzureOpenAI(
            azure_endpoint=_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2024-12-01-preview",
        )

    return _attach_live_vectors(
        docs,
        text_field=text_field,
        vector_field=vector_field,
        dimensions=dimensions,
        batch_size=batch_size,
        create_embeddings=lambda texts: aoai.embeddings.create(
            model=deployment,
            input=texts,
            dimensions=dimensions,
        ),
    )


def generate_embeddings(
    chunks: list[dict[str, Any]],
    client: Any,
    *,
    batch_size: int = 64,
    cache: dict[str, list[float]] | None = None,
    entities_by_id: dict[str, dict[str, Any]] | None = None,
    vector_field: str = "chunk_vector",
    dimensions: int = _VECTOR_DIMS,
) -> list[dict[str, Any]]:
    """Generate embeddings for chunk dicts using ``FoundryClient.embed()``.

    Builds AI Search documents via :func:`linkage.derive_chunk_doc` (composing,
    not duplicating Fenster's logic), then embeds ``embedding_text`` via
    ``client.embed()`` in batches.  Unchanged chunks (same ``content_hash``) are
    served from *cache* to avoid redundant network calls.

    Parameters
    ----------
    chunks:
        List of chunk row dicts.  Each must have at minimum ``chunk_id``,
        ``content_hash``, and ``embedding_text`` (or ``content``).
    client:
        :class:`~fabric_kg_builder.enrichment.foundry_client.FoundryClient`
        instance.  Its :meth:`embed` method is called with a ``list[str]``
        and must return ``list[list[float]]``.
    batch_size:
        Maximum texts per ``client.embed()`` call (default 64).
    cache:
        Mutable dict mapping ``content_hash`` → ``list[float]``.  Pass the
        same object across calls to persist the cache between invocations.
        When None a fresh cache is used (no cross-call reuse).
    entities_by_id:
        Optional entity lookup forwarded to :func:`linkage.derive_chunk_doc`
        for entity-denormalisation in the search docs.
    vector_field:
        Name of the vector key to write into each doc (default ``"chunk_vector"``).
    dimensions:
        Expected vector dimensions (default 1536 — must match index schema).

    Returns
    -------
    list[dict]
        AI Search document dicts, one per chunk, each with ``vector_field``
        set to a ``list[float]`` of length *dimensions*.
    """
    from .linkage import derive_chunk_doc  # compose, don't duplicate

    if cache is None:
        cache = {}

    docs = [derive_chunk_doc(chunk, entities_by_id) for chunk in chunks]
    tokenizer = TiktokenTokenizer(EMBEDDING_TOKEN_ENCODING)

    # Identify chunks whose embedding is not yet cached.
    uncached_chunk_indices: list[int] = []
    uncached_texts: list[str] = []
    for i, chunk in enumerate(chunks):
        ch = chunk.get("content_hash", "")
        if ch not in cache:
            uncached_chunk_indices.append(i)
            uncached_texts.append(
                document_embedding_text(
                    chunk,
                    text_field="embedding_text",
                    tokenizer=tokenizer,
                )
            )

    # Embed uncached texts in batches.
    for batch_start in range(0, len(uncached_texts), batch_size):
        batch_texts = uncached_texts[batch_start : batch_start + batch_size]
        batch_indices = uncached_chunk_indices[batch_start : batch_start + batch_size]
        vectors: list[list[float]] = client.embed(batch_texts)
        for idx, vector in zip(batch_indices, vectors):
            ch = chunks[idx].get("content_hash", "")
            cache[ch] = vector

    # Attach cached vectors to every doc.
    zero_vec = [0.0] * dimensions
    for doc, chunk in zip(docs, chunks):
        ch = chunk.get("content_hash", "")
        doc[vector_field] = cache.get(ch, zero_vec)

    return docs
