"""Table normalization helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from fabric_kg_builder.model.ids import content_hash as compute_content_hash
from fabric_kg_builder.model.ids import make_chunk_id, make_document_element_id
from fabric_kg_builder.model.schemas import ChunkRow, DocumentElementRow


def _plain_text(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def _extract_cells_from_row_tag(
    row_tag: Tag,
    *,
    source_file_id: str,
    table_id: str,
    page_number: int | None,
    section_path: str | None,
    sort_order_start: int,
    extracted_at: datetime,
) -> tuple[list[DocumentElementRow], int]:
    cells: list[DocumentElementRow] = []
    sort_order = sort_order_start
    row_index = 0
    if row_tag.parent is not None:
        row_tags = [tag for tag in row_tag.parent.find_all("tr", recursive=False)]
        if row_tag in row_tags:
            row_index = row_tags.index(row_tag)

    for col_index, cell_tag in enumerate(row_tag.find_all(["th", "td"], recursive=False)):
        content = cell_tag.get_text(" ", strip=True)
        if not content:
            continue
        cell_hash = compute_content_hash(content)
        cells.append(
            DocumentElementRow(
                document_element_id=make_document_element_id(
                    source_file_id,
                    "table_cell",
                    page_number,
                    sort_order,
                    cell_hash,
                ),
                source_file_id=source_file_id,
                element_type="table_cell",
                parent_element_id=table_id,
                content=content,
                content_html=str(cell_tag),
                page_number=page_number,
                section_path=section_path,
                sort_order=sort_order,
                row_index=row_index,
                col_index=col_index,
                content_hash=cell_hash,
                extracted_at=extracted_at,
            )
        )
        sort_order += 1
    return cells, sort_order


class TableExtractor:
    """Normalize table elements into cells and chunks."""

    @staticmethod
    def extract(document_elements: list[DocumentElementRow]) -> list[DocumentElementRow]:
        now = datetime.now(timezone.utc)
        next_sort = max((element.sort_order or -1) for element in document_elements) + 1
        extracted: list[DocumentElementRow] = []
        parsed_tables: set[str] = set()

        for element in document_elements:
            if element.element_type != "table" or not element.content_html:
                continue
            soup = BeautifulSoup(element.content_html, "lxml")
            for row_tag in soup.find_all("tr"):
                cells, next_sort = _extract_cells_from_row_tag(
                    row_tag,
                    source_file_id=element.source_file_id,
                    table_id=element.document_element_id,
                    page_number=element.page_number,
                    section_path=element.section_path,
                    sort_order_start=next_sort,
                    extracted_at=now,
                )
                extracted.extend(cells)
            parsed_tables.add(element.document_element_id)

        for element in document_elements:
            if element.element_type != "table_row" or not element.content_html:
                continue
            if element.parent_element_id and element.parent_element_id in parsed_tables:
                continue
            soup = BeautifulSoup(element.content_html, "lxml")
            row_tag = soup.find("tr")
            if row_tag is None:
                continue
            table_id = element.parent_element_id or element.document_element_id
            cells, next_sort = _extract_cells_from_row_tag(
                row_tag,
                source_file_id=element.source_file_id,
                table_id=table_id,
                page_number=element.page_number,
                section_path=element.section_path,
                sort_order_start=next_sort,
                extracted_at=now,
            )
            for cell in cells:
                if element.row_index is not None:
                    cell.row_index = element.row_index
            extracted.extend(cells)

        return extracted

    @staticmethod
    def extract_tables_from_html(html_string: str, source_file_id: str) -> list[DocumentElementRow]:
        now = datetime.now(timezone.utc)
        soup = BeautifulSoup(html_string, "lxml")
        elements: list[DocumentElementRow] = []
        sort_order = 0

        for table_tag in soup.find_all("table"):
            table_html = str(table_tag)
            table_text = table_tag.get_text(" ", strip=True)
            if not table_text and not table_html.strip():
                continue
            table_hash = compute_content_hash(table_text)
            table_id = make_document_element_id(source_file_id, "table", None, sort_order, table_hash)
            elements.append(
                DocumentElementRow(
                    document_element_id=table_id,
                    source_file_id=source_file_id,
                    element_type="table",
                    content=table_text,
                    content_html=table_html,
                    sort_order=sort_order,
                    content_hash=table_hash,
                    extracted_at=now,
                )
            )
            sort_order += 1

            for row_index, row_tag in enumerate(table_tag.find_all("tr")):
                row_text = row_tag.get_text(" ", strip=True)
                if not row_text:
                    continue
                row_hash = compute_content_hash(row_text)
                row_id = make_document_element_id(source_file_id, "table_row", None, sort_order, row_hash)
                row_element = DocumentElementRow(
                    document_element_id=row_id,
                    source_file_id=source_file_id,
                    element_type="table_row",
                    parent_element_id=table_id,
                    content=row_text,
                    content_html=str(row_tag),
                    sort_order=sort_order,
                    row_index=row_index,
                    content_hash=row_hash,
                    extracted_at=now,
                )
                elements.append(row_element)
                sort_order += 1

                cells, sort_order = _extract_cells_from_row_tag(
                    row_tag,
                    source_file_id=source_file_id,
                    table_id=table_id,
                    page_number=None,
                    section_path=None,
                    sort_order_start=sort_order,
                    extracted_at=now,
                )
                for cell in cells:
                    cell.row_index = row_index
                elements.extend(cells)

        return elements

    @staticmethod
    def table_html_chunks(
        table_elements: list[DocumentElementRow],
        source_file_id: str,
    ) -> list[ChunkRow]:
        now = datetime.now(timezone.utc)
        chunks: list[ChunkRow] = []
        rows_by_parent: dict[str, list[DocumentElementRow]] = {}
        for element in table_elements:
            if element.element_type == "table_row" and element.parent_element_id:
                rows_by_parent.setdefault(element.parent_element_id, []).append(element)

        seen_row_ids: set[str] = set()
        for element in table_elements:
            if element.element_type != "table":
                continue
            plain_text = element.content or _plain_text(element.content_html)
            if not plain_text and not (element.content_html or "").strip():
                continue
            table_hash = compute_content_hash(plain_text)
            chunks.append(
                ChunkRow(
                    chunk_id=make_chunk_id(source_file_id, "table_html", table_hash),
                    source_file_id=source_file_id,
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

            rows = rows_by_parent.get(element.document_element_id)
            if rows:
                for row in rows:
                    row_text = (row.content or _plain_text(row.content_html)).strip()
                    if not row_text:
                        continue
                    row_hash = compute_content_hash(row_text)
                    chunks.append(
                        ChunkRow(
                            chunk_id=make_chunk_id(source_file_id, "table_row", row_hash),
                            source_file_id=source_file_id,
                            document_element_id=row.document_element_id,
                            chunk_type="table_row",
                            content=row_text,
                            content_html=row.content_html,
                            embedding_text=row_text,
                            page_number=row.page_number,
                            section_path=row.section_path,
                            table_id=element.document_element_id,
                            content_hash=row_hash,
                            created_at=now,
                        )
                    )
                    seen_row_ids.add(row.document_element_id)
            elif element.content_html:
                soup = BeautifulSoup(element.content_html, "lxml")
                for row_tag in soup.find_all("tr"):
                    row_text = row_tag.get_text(" ", strip=True)
                    if not row_text:
                        continue
                    row_hash = compute_content_hash(row_text)
                    chunks.append(
                        ChunkRow(
                            chunk_id=make_chunk_id(source_file_id, "table_row", row_hash),
                            source_file_id=source_file_id,
                            chunk_type="table_row",
                            content=row_text,
                            content_html=str(row_tag),
                            embedding_text=row_text,
                            page_number=element.page_number,
                            section_path=element.section_path,
                            table_id=element.document_element_id,
                            content_hash=row_hash,
                            created_at=now,
                        )
                    )

        for element in table_elements:
            if element.element_type != "table_row":
                continue
            if element.document_element_id in seen_row_ids:
                continue
            row_text = (element.content or _plain_text(element.content_html)).strip()
            if not row_text:
                continue
            row_hash = compute_content_hash(row_text)
            chunks.append(
                ChunkRow(
                    chunk_id=make_chunk_id(source_file_id, "table_row", row_hash),
                    source_file_id=source_file_id,
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

        return chunks
