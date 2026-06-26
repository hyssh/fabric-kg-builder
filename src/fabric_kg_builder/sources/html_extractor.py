"""HTML extractor built on BeautifulSoup."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from fabric_kg_builder.model.ids import (
    content_hash as compute_content_hash,
    make_document_element_id,
    make_source_file_id,
)
from fabric_kg_builder.model.schemas import DocumentElementRow, SourceFileRow


class HtmlExtractResult:
    """Result returned by :meth:`HtmlExtractor.extract`."""

    __slots__ = ("source_file", "document_elements")

    def __init__(
        self,
        source_file: SourceFileRow,
        document_elements: list[DocumentElementRow],
    ) -> None:
        self.source_file = source_file
        self.document_elements = document_elements


def _canonical_path(path: Path, project_root: Path | None) -> str:
    root = Path(project_root) if project_root else Path.cwd()
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


class HtmlExtractor:
    """Extract sections, paragraphs, tables, and images from HTML content."""

    @staticmethod
    def extract(
        source: str | Path,
        *,
        project_root: str | Path | None = None,
    ) -> HtmlExtractResult:
        now = datetime.now(timezone.utc)
        raw_html: str
        filename: str
        byte_size: int
        canonical_path: str

        if isinstance(source, Path) or Path(str(source)).exists():
            path = Path(source)
            raw_bytes = path.read_bytes()
            raw_html = raw_bytes.decode("utf-8", errors="ignore")
            filename = path.name
            byte_size = path.stat().st_size
            canonical_path = _canonical_path(path, Path(project_root) if project_root else None)
            file_hash = hashlib.sha256(raw_bytes).hexdigest()
        else:
            raw_html = str(source)
            raw_bytes = raw_html.encode("utf-8")
            filename = "inline.html"
            byte_size = len(raw_bytes)
            canonical_path = "inline.html"
            file_hash = hashlib.sha256(raw_bytes).hexdigest()

        source_file_id = make_source_file_id(canonical_path, file_hash)
        source_file = SourceFileRow(
            source_file_id=source_file_id,
            path=canonical_path,
            filename=filename,
            source_type="html",
            content_hash=file_hash,
            byte_size=byte_size,
            ingested_at=now,
        )

        soup = BeautifulSoup(raw_html, "lxml")
        document_elements: list[DocumentElementRow] = []
        heading_stack: list[str] = []
        heading_id_stack: list[str] = []
        sort_order = 0

        for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "table", "img"]):
            if not isinstance(tag, Tag):
                continue
            if tag.name in {"h1", "h2", "h3", "h4", "p"} and tag.find_parent("table") is not None:
                continue

            if tag.name in {"h1", "h2", "h3", "h4"}:
                text = tag.get_text(" ", strip=True)
                if not text:
                    continue
                level = int(tag.name[1])
                heading_stack = heading_stack[: level - 1]
                heading_id_stack = heading_id_stack[: level - 1]
                heading_stack.append(text)
                section_path = "/".join(heading_stack)
                section_hash = compute_content_hash(text)
                parent_element_id = heading_id_stack[-1] if heading_id_stack else None
                section_id = make_document_element_id(
                    source_file_id,
                    "section",
                    None,
                    sort_order,
                    section_hash,
                )
                document_elements.append(
                    DocumentElementRow(
                        document_element_id=section_id,
                        source_file_id=source_file_id,
                        element_type="section",
                        parent_element_id=parent_element_id,
                        title=text,
                        content=text,
                        section_path=section_path,
                        sort_order=sort_order,
                        content_hash=section_hash,
                        extracted_at=now,
                    )
                )
                heading_id_stack.append(section_id)
                sort_order += 1
                continue

            current_section_path = "/".join(heading_stack) if heading_stack else None
            current_parent_id = heading_id_stack[-1] if heading_id_stack else None

            if tag.name == "p":
                text = tag.get_text(" ", strip=True)
                if not text:
                    continue
                paragraph_hash = compute_content_hash(text)
                document_elements.append(
                    DocumentElementRow(
                        document_element_id=make_document_element_id(
                            source_file_id,
                            "paragraph",
                            None,
                            sort_order,
                            paragraph_hash,
                        ),
                        source_file_id=source_file_id,
                        element_type="paragraph",
                        parent_element_id=current_parent_id,
                        content=text,
                        section_path=current_section_path,
                        sort_order=sort_order,
                        content_hash=paragraph_hash,
                        extracted_at=now,
                    )
                )
                sort_order += 1
                continue

            if tag.name == "table":
                table_html = str(tag)
                table_text = tag.get_text(" ", strip=True)
                if not table_text and not table_html.strip():
                    continue
                table_hash = compute_content_hash(table_text)
                document_elements.append(
                    DocumentElementRow(
                        document_element_id=make_document_element_id(
                            source_file_id,
                            "table",
                            None,
                            sort_order,
                            table_hash,
                        ),
                        source_file_id=source_file_id,
                        element_type="table",
                        parent_element_id=current_parent_id,
                        content=table_text,
                        content_html=table_html,
                        section_path=current_section_path,
                        sort_order=sort_order,
                        content_hash=table_hash,
                        extracted_at=now,
                    )
                )
                sort_order += 1
                continue

            if tag.name == "img":
                src = tag.get("src", "").strip()
                alt = tag.get("alt", "").strip()
                content = " ".join(part for part in (src, alt) if part).strip()
                if not content:
                    continue
                image_hash = compute_content_hash(content)
                document_elements.append(
                    DocumentElementRow(
                        document_element_id=make_document_element_id(
                            source_file_id,
                            "image_ref",
                            None,
                            sort_order,
                            image_hash,
                        ),
                        source_file_id=source_file_id,
                        element_type="image_ref",
                        parent_element_id=current_parent_id,
                        content=content,
                        section_path=current_section_path,
                        sort_order=sort_order,
                        content_hash=image_hash,
                        extracted_at=now,
                    )
                )
                sort_order += 1

        return HtmlExtractResult(source_file=source_file, document_elements=document_elements)
