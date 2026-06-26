"""PDF extractor built on pdfplumber."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import pdfplumber

from fabric_kg_builder.model.ids import (
    content_hash as compute_content_hash,
    make_document_element_id,
    make_source_file_id,
)
from fabric_kg_builder.model.schemas import DocumentElementRow, SourceFileRow

_NUMBERED_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+[A-Z]")


class PdfExtractResult:
    """Result returned by :meth:`PdfExtractor.extract`."""

    __slots__ = ("source_file", "document_elements", "page_count")

    def __init__(
        self,
        source_file: SourceFileRow,
        document_elements: list[DocumentElementRow],
        page_count: int,
    ) -> None:
        self.source_file = source_file
        self.document_elements = document_elements
        self.page_count = page_count


def _canonical_path(path: Path, project_root: Path | None) -> str:
    root = Path(project_root) if project_root else Path.cwd()
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _file_content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_images(path: str | Path) -> list[str]:
    """Placeholder image extraction hook for future visual asset support."""
    _ = path
    return []


def _group_words_into_lines(words: list[dict]) -> list[dict[str, float | str]]:
    if not words:
        return []

    sorted_words = sorted(
        words,
        key=lambda word: (
            float(word.get("top", 0.0)),
            float(word.get("x0", 0.0)),
        ),
    )

    lines: list[list[dict]] = []
    current_line: list[dict] = []
    current_top: float | None = None
    tolerance = 3.0

    for word in sorted_words:
        top = float(word.get("top", 0.0))
        if current_top is None or abs(top - current_top) <= tolerance:
            current_line.append(word)
            current_top = top if current_top is None else (current_top + top) / 2
            continue
        lines.append(current_line)
        current_line = [word]
        current_top = top

    if current_line:
        lines.append(current_line)

    result: list[dict[str, float | str]] = []
    for line_words in lines:
        text = " ".join(str(word.get("text", "")).strip() for word in line_words).strip()
        if not text:
            continue
        sizes = [float(word.get("size", 0.0) or 0.0) for word in line_words]
        tops = [float(word.get("top", 0.0)) for word in line_words]
        result.append(
            {
                "text": text,
                "avg_size": sum(sizes) / len(sizes) if sizes else 0.0,
                "top": sum(tops) / len(tops) if tops else 0.0,
            }
        )
    return result


def _is_all_caps_heading(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    return bool(letters) and all(char.isupper() for char in letters)


def _is_heading(text: str, avg_size: float, median_size: float) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if median_size and avg_size >= median_size * 1.2:
        return True
    if _NUMBERED_HEADING_RE.match(stripped):
        return True
    if len(stripped) < 100 and _is_all_caps_heading(stripped):
        return True
    return stripped.endswith(":") and len(stripped) < 120


def _heading_level(text: str, avg_size: float, median_size: float) -> int:
    numbered = re.match(r"^(\d+(?:\.\d+)*)\.?\s+", text.strip())
    if numbered:
        return min(numbered.group(1).count(".") + 1, 4)
    if _is_all_caps_heading(text) and len(text) < 100:
        return 1
    ratio = (avg_size / median_size) if median_size else 1.0
    if ratio >= 1.6:
        return 1
    if ratio >= 1.35:
        return 2
    if text.strip().endswith(":"):
        return 2
    return 3


class PdfExtractor:
    """Extract PDF document elements with page/section/paragraph structure."""

    @staticmethod
    def extract(path: str | Path, *, project_root: str | Path | None = None) -> PdfExtractResult:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Source file not found: {path}")

        file_hash = _file_content_hash(path)
        canonical_path = _canonical_path(path, Path(project_root) if project_root else None)
        source_file_id = make_source_file_id(canonical_path, file_hash)
        now = datetime.now(timezone.utc)

        source_file = SourceFileRow(
            source_file_id=source_file_id,
            path=canonical_path,
            filename=path.name,
            source_type="pdf",
            content_hash=file_hash,
            byte_size=path.stat().st_size,
            ingested_at=now,
        )

        document_elements: list[DocumentElementRow] = []
        sort_order = 0
        heading_stack: list[str] = []
        heading_id_stack: list[str] = []

        with pdfplumber.open(path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                page_text = (page.extract_text() or "").strip()
                page_hash = compute_content_hash(page_text)
                page_element_id = make_document_element_id(
                    source_file_id,
                    "page",
                    page_number,
                    sort_order,
                    page_hash,
                )
                document_elements.append(
                    DocumentElementRow(
                        document_element_id=page_element_id,
                        source_file_id=source_file_id,
                        element_type="page",
                        content=page_text,
                        page_number=page_number,
                        sort_order=sort_order,
                        content_hash=page_hash,
                        extracted_at=now,
                    )
                )
                sort_order += 1

                try:
                    words = page.extract_words(extra_attrs=["size"])
                except Exception:  # pragma: no cover - pdfplumber backend variance
                    words = []
                lines = _group_words_into_lines(words)
                page_sizes = [float(char.get("size", 0.0) or 0.0) for char in (page.chars or [])]
                median_size = float(median(page_sizes)) if page_sizes else 0.0
                line_tops = [float(line["top"]) for line in lines]
                top_gaps = [
                    line_tops[index] - line_tops[index - 1]
                    for index in range(1, len(line_tops))
                    if line_tops[index] > line_tops[index - 1]
                ]
                median_gap = float(median(top_gaps)) if top_gaps else 0.0

                paragraph_lines: list[str] = []
                paragraph_parent_id: str | None = heading_id_stack[-1] if heading_id_stack else None
                paragraph_section_path = "/".join(heading_stack) if heading_stack else None
                previous_top: float | None = None

                def flush_paragraph() -> None:
                    nonlocal sort_order, paragraph_lines
                    content = "\n".join(line.strip() for line in paragraph_lines if line.strip()).strip()
                    paragraph_lines = []
                    if not content:
                        return
                    paragraph_hash = compute_content_hash(content)
                    document_elements.append(
                        DocumentElementRow(
                            document_element_id=make_document_element_id(
                                source_file_id,
                                "paragraph",
                                page_number,
                                sort_order,
                                paragraph_hash,
                            ),
                            source_file_id=source_file_id,
                            element_type="paragraph",
                            parent_element_id=paragraph_parent_id,
                            content=content,
                            page_number=page_number,
                            section_path=paragraph_section_path,
                            sort_order=sort_order,
                            content_hash=paragraph_hash,
                            extracted_at=now,
                        )
                    )
                    sort_order += 1

                for line in lines:
                    text = str(line["text"]).strip()
                    avg_size = float(line["avg_size"])
                    top = float(line["top"])
                    if not text:
                        continue

                    if previous_top is not None and median_gap and (top - previous_top) > (median_gap * 1.8):
                        flush_paragraph()
                    previous_top = top

                    if _is_heading(text, avg_size, median_size):
                        flush_paragraph()
                        level = _heading_level(text, avg_size, median_size)
                        heading_stack = heading_stack[: level - 1]
                        heading_id_stack = heading_id_stack[: level - 1]
                        heading_stack.append(text)
                        section_path = "/".join(heading_stack)
                        section_hash = compute_content_hash(text)
                        parent_element_id = heading_id_stack[-1] if heading_id_stack else None
                        section_id = make_document_element_id(
                            source_file_id,
                            "section",
                            page_number,
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
                                page_number=page_number,
                                section_path=section_path,
                                sort_order=sort_order,
                                content_hash=section_hash,
                                extracted_at=now,
                            )
                        )
                        sort_order += 1
                        heading_id_stack.append(section_id)
                        paragraph_parent_id = heading_id_stack[-1]
                        paragraph_section_path = section_path
                        continue

                    paragraph_parent_id = heading_id_stack[-1] if heading_id_stack else None
                    paragraph_section_path = "/".join(heading_stack) if heading_stack else None
                    paragraph_lines.append(text)

                flush_paragraph()

            return PdfExtractResult(
                source_file=source_file,
                document_elements=document_elements,
                page_count=len(pdf.pages),
            )

    @staticmethod
    def extract_images(path: str | Path) -> list[str]:
        return extract_images(path)
