"""Token-aware structural chunking with measured adjacent overlap."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Protocol

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, model_validator

from fabric_kg_builder.model.ids import (
    content_hash as compute_content_hash,
    make_chunk_lineage_id,
)
from fabric_kg_builder.model.schemas import ChunkRow, DocumentElementRow


class TokenChunkStrategy(BaseModel):
    """Versioned embedding-aware chunk strategy."""

    version: str = "token-window-v2"
    encoding_name: str = "cl100k_base"
    target_tokens: int = Field(default=800, ge=32)
    max_tokens: int = Field(default=1_000, ge=32)
    model_limit: int = Field(default=8_191, ge=64)
    reserve_tokens: int = Field(default=64, ge=0)
    overlap_ratio: float = Field(default=0.05, ge=0.0, le=0.5)

    @model_validator(mode="after")
    def validate_budget(self) -> "TokenChunkStrategy":
        usable_limit = self.model_limit - self.reserve_tokens
        if usable_limit <= 0:
            raise ValueError("reserve_tokens must be smaller than model_limit")
        if self.max_tokens > usable_limit:
            raise ValueError(
                "max_tokens must fit within model_limit - reserve_tokens"
            )
        if self.target_tokens > self.max_tokens:
            raise ValueError("target_tokens must not exceed max_tokens")
        overlap_tokens = round(self.target_tokens * self.overlap_ratio)
        if overlap_tokens >= self.target_tokens:
            raise ValueError("overlap must leave a positive chunk stride")
        return self

    @property
    def overlap_tokens(self) -> int:
        return round(self.target_tokens * self.overlap_ratio)


class Tokenizer(Protocol):
    def encode(self, text: str) -> list[int]: ...

    def decode(self, tokens: list[int]) -> str: ...


class TiktokenTokenizer:
    """Tokenizer backed by the same encoding family used by embedding models."""

    def __init__(self, encoding_name: str) -> None:
        try:
            import tiktoken
        except ImportError as exc:  # pragma: no cover - dependency installation
            raise ImportError(
                "Token-aware chunking requires tiktoken. "
                "Install project dependencies before running enrichment."
            ) from exc
        self._encoding = tiktoken.get_encoding(encoding_name)

    def encode(self, text: str) -> list[int]:
        return self._encoding.encode(text)

    def decode(self, tokens: list[int]) -> str:
        return self._encoding.decode(tokens)


class ChunkResult:
    """Result returned by :meth:`Chunker.extract`."""

    __slots__ = ("chunks", "strategy")

    def __init__(
        self,
        chunks: list[ChunkRow],
        strategy: TokenChunkStrategy,
    ) -> None:
        self.chunks = chunks
        self.strategy = strategy


def _plain_text_from_html(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def _chunk_type(element: DocumentElementRow, content: str) -> str | None:
    if element.element_type == "page":
        return "raw_page_text"
    if element.element_type == "table":
        return "table_html"
    if element.element_type == "table_row":
        return "table_row"
    if element.element_type == "table_cell":
        return "table_cell"
    if element.element_type in {
        "section",
        "paragraph",
        "slide_text",
        "slide_notes",
        "parquet_row",
        "ocr_text",
        "drawing_text",
        "drawing_note",
        "drawing_callout",
    }:
        if content.startswith("WARNING") or content.startswith("⚠"):
            return "warning"
        if content.startswith("NOTE") or content.startswith("Note:"):
            return "note"
        return "section_text"
    return None


def _element_content(element: DocumentElementRow) -> str:
    content = (element.content or "").strip()
    if element.element_type == "section" and not content:
        content = (element.title or "").strip()
    if element.element_type in {"table", "table_row", "table_cell"} and not content:
        content = _plain_text_from_html(element.content_html)
    return content


def _source_locator(
    element: DocumentElementRow,
    *,
    token_start: int,
    token_end: int,
    overlap_token_count: int,
    strategy: TokenChunkStrategy,
) -> str:
    element_locator: object | None = None
    if element.source_locator_json:
        try:
            element_locator = json.loads(element.source_locator_json)
        except json.JSONDecodeError:
            element_locator = element.source_locator_json
    return json.dumps(
        {
            "document_element_id": element.document_element_id,
            "element_locator": element_locator,
            "token_start": token_start,
            "token_end": token_end,
            "overlap_token_count": overlap_token_count,
            "encoding": strategy.encoding_name,
            "chunk_strategy_version": strategy.version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _lineage_update(element: DocumentElementRow) -> dict[str, object]:
    return {
        "project_id": element.project_id,
        "asset_id": element.asset_id,
        "asset_version_id": element.asset_version_id,
        "run_id": element.run_id,
        "parent_record_id": element.document_element_id,
        "schema_version": element.schema_version,
        "domain_hash": element.domain_hash,
    }


def _token_windows(
    text: str,
    *,
    strategy: TokenChunkStrategy,
    tokenizer: Tokenizer,
) -> list[tuple[str, int, int, int, int]]:
    """Return text, token start/end, overlap, and retokenized count."""
    tokens = tokenizer.encode(text)
    if not tokens:
        return []

    target = min(strategy.target_tokens, strategy.max_tokens)
    overlap = min(strategy.overlap_tokens, max(target - 1, 0))
    stride = target - overlap
    windows: list[tuple[str, int, int, int, int]] = []

    start = 0
    ordinal = 0
    while start < len(tokens):
        end = min(start + target, len(tokens))
        window_tokens = tokens[start:end]
        window_text = tokenizer.decode(window_tokens).strip()
        if window_text:
            retokenized_count = len(tokenizer.encode(window_text))
            if retokenized_count > strategy.max_tokens:
                raise ValueError(
                    "Decoded chunk exceeds max_tokens after retokenization: "
                    f"{retokenized_count} > {strategy.max_tokens}"
                )
            windows.append(
                (
                    window_text,
                    start,
                    end,
                    0 if ordinal == 0 else min(overlap, end - start),
                    retokenized_count,
                )
            )
            ordinal += 1
        if end >= len(tokens):
            break
        start += stride

    return windows


class Chunker:
    """Chunk structural elements into embedding-safe records."""

    @staticmethod
    def extract(
        document_elements: list[DocumentElementRow],
        *,
        strategy: TokenChunkStrategy | None = None,
        tokenizer: Tokenizer | None = None,
    ) -> ChunkResult:
        strategy = strategy or TokenChunkStrategy()
        tokenizer = tokenizer or TiktokenTokenizer(strategy.encoding_name)
        now = datetime.now(timezone.utc)
        chunks: list[ChunkRow] = []

        ordered_elements = sorted(
            document_elements,
            key=lambda element: (
                element.source_file_id,
                element.sort_order if element.sort_order is not None else 2**31,
                element.document_element_id,
            ),
        )

        for element in ordered_elements:
            content = _element_content(element)
            if not content:
                continue
            chunk_type = _chunk_type(element, content)
            if chunk_type is None:
                continue

            windows = _token_windows(
                content,
                strategy=strategy,
                tokenizer=tokenizer,
            )
            for ordinal, (
                part,
                token_start,
                token_end,
                overlap_token_count,
                token_count,
            ) in enumerate(windows):
                part_hash = compute_content_hash(part)
                chunk_id = make_chunk_lineage_id(
                    element.document_element_id,
                    strategy.version,
                    ordinal,
                    part_hash,
                )
                chunks.append(
                    ChunkRow(
                        chunk_id=chunk_id,
                        source_file_id=element.source_file_id,
                        document_element_id=element.document_element_id,
                        chunk_type=chunk_type,
                        content=part,
                        content_html=(
                            element.content_html
                            if element.element_type in {"table", "table_row", "table_cell"}
                            else None
                        ),
                        embedding_text=part,
                        blob_url=element.blob_url,
                        page_number=element.page_number,
                        section_path=element.section_path,
                        table_id=(
                            element.document_element_id
                            if element.element_type == "table"
                            else element.parent_element_id
                            if element.element_type in {"table_row", "table_cell"}
                            else None
                        ),
                        content_hash=part_hash,
                        created_at=now,
                        token_count=token_count,
                        token_start=token_start,
                        token_end=token_end,
                        overlap_token_count=overlap_token_count,
                        chunk_strategy_version=strategy.version,
                        source_locator_json=_source_locator(
                            element,
                            token_start=token_start,
                            token_end=token_end,
                            overlap_token_count=overlap_token_count,
                            strategy=strategy,
                        ),
                        **_lineage_update(element),
                    )
                )

        by_source: dict[str, list[int]] = {}
        for index, chunk in enumerate(chunks):
            by_source.setdefault(chunk.source_file_id, []).append(index)
        for indexes in by_source.values():
            for position, index in enumerate(indexes):
                previous_id = chunks[indexes[position - 1]].chunk_id if position else None
                next_id = (
                    chunks[indexes[position + 1]].chunk_id
                    if position + 1 < len(indexes)
                    else None
                )
                chunks[index] = chunks[index].model_copy(
                    update={
                        "previous_chunk_id": previous_id,
                        "next_chunk_id": next_id,
                    }
                )

        return ChunkResult(chunks=chunks, strategy=strategy)
