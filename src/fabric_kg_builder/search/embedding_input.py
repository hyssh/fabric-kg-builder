"""Shared token boundary for direct and integrated embedding inputs."""

from __future__ import annotations

from typing import Any, Protocol

from fabric_kg_builder.sources.chunker import TiktokenTokenizer

EMBEDDING_INPUT_TOKEN_LIMIT = 7_900
EMBEDDING_TOKEN_ENCODING = "cl100k_base"
EMBEDDING_INPUT_VERSION = "1"
EMPTY_EMBEDDING_TEXT = "Non-textual source record."

_FALLBACK_FIELDS = (
    "content",
    "content_html",
    "title",
    "name",
    "caption",
    "description",
    "summary",
    "section_path",
    "element_type",
    "asset_type",
    "region_type",
    "record_type",
    "content_type",
    "chunk_type",
)


class _Tokenizer(Protocol):
    def encode(self, text: str) -> list[int]: ...

    def decode(self, tokens: list[int]) -> str: ...


def bound_embedding_text(
    embedding_text: str,
    *,
    content: str = "",
    tokenizer: _Tokenizer | None = None,
) -> str:
    """Return provider-safe text without mutating the authoritative document."""
    active_tokenizer = tokenizer or TiktokenTokenizer(EMBEDDING_TOKEN_ENCODING)
    if not embedding_text.strip():
        embedding_text = content if content.strip() else EMPTY_EMBEDDING_TEXT
    embedding_tokens = active_tokenizer.encode(embedding_text)
    if len(embedding_tokens) <= EMBEDDING_INPUT_TOKEN_LIMIT:
        return embedding_text

    if content.strip():
        content_tokens = active_tokenizer.encode(content)
        if len(content_tokens) <= EMBEDDING_INPUT_TOKEN_LIMIT:
            return content

    return active_tokenizer.decode(
        embedding_tokens[:EMBEDDING_INPUT_TOKEN_LIMIT]
    )


def document_embedding_text(
    document: dict[str, Any],
    *,
    text_field: str,
    tokenizer: _Tokenizer | None = None,
) -> str:
    """Build non-empty, bounded provider input from a Search document."""
    primary = str(document.get(text_field) or "")
    fallback_values: list[str] = []
    seen = {primary}
    for field_name in _FALLBACK_FIELDS:
        value = str(document.get(field_name) or "")
        if value.strip() and value not in seen:
            seen.add(value)
            fallback_values.append(value)
    return bound_embedding_text(
        primary,
        content="\n".join(fallback_values),
        tokenizer=tokenizer,
    )
