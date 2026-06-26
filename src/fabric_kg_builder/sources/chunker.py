"""Convert document elements into chunk rows."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from fabric_kg_builder.model.ids import content_hash as compute_content_hash
from fabric_kg_builder.model.ids import make_chunk_id
from fabric_kg_builder.model.schemas import ChunkRow, DocumentElementRow


class ChunkResult:
    """Result returned by :meth:`Chunker.extract`."""

    __slots__ = ("chunks",)

    def __init__(self, chunks: list[ChunkRow]) -> None:
        self.chunks = chunks


def _plain_text_from_html(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def _split_text(text: str, *, target_size: int = 1000, max_size: int = 1500) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_size:
        return [cleaned]

    parts = [part.strip() for part in re.split(r"\n\s*\n", cleaned) if part.strip()]
    if len(parts) == 1:
        parts = [part.strip() for part in cleaned.splitlines() if part.strip()]
    if not parts:
        return [cleaned]

    chunks: list[str] = []
    current = ""
    for part in parts:
        separator = "\n\n" if current else ""
        candidate = f"{current}{separator}{part}" if current else part
        if len(candidate) <= target_size or not current:
            current = candidate
            if len(current) <= max_size:
                continue

        if current:
            if len(current) > max_size:
                for index in range(0, len(current), target_size):
                    chunks.append(current[index : index + target_size].strip())
            else:
                chunks.append(current.strip())
        current = part

    if current:
        if len(current) > max_size:
            for index in range(0, len(current), target_size):
                chunks.append(current[index : index + target_size].strip())
        else:
            chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


class Chunker:
    """Chunk document elements into search/index friendly records."""

    @staticmethod
    def extract(document_elements: list[DocumentElementRow]) -> ChunkResult:
        now = datetime.now(timezone.utc)
        chunks: list[ChunkRow] = []

        for element in document_elements:
            content = (element.content or "").strip()
            if element.element_type == "section" and not content:
                content = (element.title or "").strip()

            if element.element_type in {"section", "paragraph"}:
                if not content:
                    continue
                chunk_type = "section_text"
                if content.startswith("WARNING") or content.startswith("⚠"):
                    chunk_type = "warning"
                elif content.startswith("NOTE") or content.startswith("Note:"):
                    chunk_type = "note"

                for part in _split_text(content):
                    part_hash = compute_content_hash(part)
                    chunks.append(
                        ChunkRow(
                            chunk_id=make_chunk_id(element.source_file_id, chunk_type, part_hash),
                            source_file_id=element.source_file_id,
                            document_element_id=element.document_element_id,
                            chunk_type=chunk_type,
                            content=part,
                            embedding_text=part,
                            page_number=element.page_number,
                            section_path=element.section_path,
                            content_hash=part_hash,
                            created_at=now,
                        )
                    )
                continue

            if element.element_type == "page":
                if not content:
                    continue
                page_hash = compute_content_hash(content)
                chunks.append(
                    ChunkRow(
                        chunk_id=make_chunk_id(element.source_file_id, "raw_page_text", page_hash),
                        source_file_id=element.source_file_id,
                        document_element_id=element.document_element_id,
                        chunk_type="raw_page_text",
                        content=content,
                        embedding_text=content,
                        page_number=element.page_number,
                        section_path=element.section_path,
                        content_hash=page_hash,
                        created_at=now,
                    )
                )
                continue

            if element.element_type == "table":
                plain_text = content or _plain_text_from_html(element.content_html)
                if not plain_text and not (element.content_html or "").strip():
                    continue
                table_hash = compute_content_hash(plain_text)
                chunks.append(
                    ChunkRow(
                        chunk_id=make_chunk_id(element.source_file_id, "table_html", table_hash),
                        source_file_id=element.source_file_id,
                        document_element_id=element.document_element_id,
                        chunk_type="table_html",
                        content=plain_text,
                        content_html=element.content_html,
                        embedding_text=plain_text,
                        page_number=element.page_number,
                        section_path=element.section_path,
                        table_id=element.document_element_id,
                        content_hash=table_hash,
                        created_at=now,
                    )
                )
                continue

            if element.element_type == "table_row":
                row_text = content or _plain_text_from_html(element.content_html)
                if not row_text:
                    continue
                row_hash = compute_content_hash(row_text)
                chunks.append(
                    ChunkRow(
                        chunk_id=make_chunk_id(element.source_file_id, "table_row", row_hash),
                        source_file_id=element.source_file_id,
                        document_element_id=element.document_element_id,
                        chunk_type="table_row",
                        content=row_text,
                        content_html=element.content_html,
                        embedding_text=row_text,
                        page_number=element.page_number,
                        section_path=element.section_path,
                        table_id=element.parent_element_id,
                        content_hash=row_hash,
                        created_at=now,
                    )
                )

        return ChunkResult(chunks=chunks)
