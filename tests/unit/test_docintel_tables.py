"""Unit tests for fabric_kg_builder.enrichment.docintel_tables.

Tests (all mocked — no live DI calls):
- table_to_html: produces <table> with <thead>/<tbody>, correct cell values
- extract_tables: yields 1 DocumentElementRow (element_type=table) + 1 ChunkRow
  (chunk_type=table_html) from a 3-row fixture table
- Deterministic IDs: same inputs → same IDs across repeated calls
- write_table_artifacts: writes table_0.html (and .md) to the build dir
- get_document_markdown: returns analyze_result.content as-is
- DocIntelClient.analyze_document_bytes: passes output_content_format to SDK
- Fixture-based integration: conftest tables mock shapes extracted correctly
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fabric_kg_builder.config.schema import DocumentIntelligenceConfig
from fabric_kg_builder.enrichment.docintel import DocIntelClient
from fabric_kg_builder.enrichment.docintel_tables import (
    DocIntelTableResult,
    _get_table_page_number,
    _table_to_embedding_text,
    _table_to_plain_text,
    extract_tables,
    get_document_markdown,
    table_to_html,
    write_table_artifacts,
)
from fabric_kg_builder.model.ids import content_hash, make_chunk_id, make_document_element_id

from tests.conftest import make_document_intelligence_client_with_tables

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_SOURCE_FILE_ID = "src:abc123deadbeef000000000000000000"
_NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
_DI_CONFIG = DocumentIntelligenceConfig(endpoint="https://fake-docintel.cognitiveservices.azure.com/")

# ---------------------------------------------------------------------------
# Minimal in-memory table fixture (3 rows: 1 header + 2 data)
# Matches: Part | Part Number | Quantity / Battery | M1287099-003 | 1
# ---------------------------------------------------------------------------

_CELLS = [
    {"row_index": 0, "column_index": 0, "content": "Part", "kind": "columnHeader",
     "bounding_regions": [{"page_number": 2, "polygon": []}]},
    {"row_index": 0, "column_index": 1, "content": "Part Number", "kind": "columnHeader",
     "bounding_regions": [{"page_number": 2, "polygon": []}]},
    {"row_index": 0, "column_index": 2, "content": "Quantity", "kind": "columnHeader",
     "bounding_regions": [{"page_number": 2, "polygon": []}]},
    {"row_index": 1, "column_index": 0, "content": "Battery",
     "bounding_regions": [{"page_number": 2, "polygon": []}]},
    {"row_index": 1, "column_index": 1, "content": "M1287099-003",
     "bounding_regions": [{"page_number": 2, "polygon": []}]},
    {"row_index": 1, "column_index": 2, "content": "1",
     "bounding_regions": [{"page_number": 2, "polygon": []}]},
    {"row_index": 2, "column_index": 0, "content": "Display",
     "bounding_regions": [{"page_number": 2, "polygon": []}]},
    {"row_index": 2, "column_index": 1, "content": "M1234567-001",
     "bounding_regions": [{"page_number": 2, "polygon": []}]},
    {"row_index": 2, "column_index": 2, "content": "1",
     "bounding_regions": [{"page_number": 2, "polygon": []}]},
]

_TABLE = {"row_count": 3, "column_count": 3, "cells": _CELLS}


def _make_di_result(tables=None, content="# Parts List\n\nTable content."):
    """Build a minimal MagicMock DI analyze_result."""
    mock = MagicMock()
    mock.tables = tables if tables is not None else [_TABLE]
    mock.content = content
    mock.pages = [{"page_number": 1, "width": 612.0, "height": 792.0, "unit": "pixel"}]
    mock.paragraphs = []
    return mock


# ---------------------------------------------------------------------------
# Tests: table_to_html
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_table_to_html_contains_table_tag():
    html = table_to_html(_TABLE)
    assert "<table>" in html
    assert "</table>" in html


@pytest.mark.unit
def test_table_to_html_has_thead_with_th_cells():
    html = table_to_html(_TABLE)
    assert "<thead>" in html
    assert "</thead>" in html
    assert "<th>Part</th>" in html
    assert "<th>Part Number</th>" in html
    assert "<th>Quantity</th>" in html


@pytest.mark.unit
def test_table_to_html_has_tbody_with_td_cells():
    html = table_to_html(_TABLE)
    assert "<tbody>" in html
    assert "</tbody>" in html
    assert "<td>Battery</td>" in html
    assert "<td>M1287099-003</td>" in html
    assert "<td>1</td>" in html


@pytest.mark.unit
def test_table_to_html_has_two_body_rows():
    html = table_to_html(_TABLE)
    # 2 data rows
    assert html.count("<tr>") >= 3  # 1 header + 2 body
    assert "<td>Display</td>" in html
    assert "<td>M1234567-001</td>" in html


@pytest.mark.unit
def test_table_to_html_no_content_columns_falls_back_gracefully():
    """Table with column_count=0 should derive count from cells."""
    minimal_table = {
        "row_count": 1,
        "column_count": 0,  # missing → derive from cells
        "cells": [
            {"row_index": 0, "column_index": 0, "content": "A", "kind": "columnHeader"},
            {"row_index": 0, "column_index": 1, "content": "B", "kind": "columnHeader"},
        ],
    }
    html = table_to_html(minimal_table)
    assert "<th>A</th>" in html
    assert "<th>B</th>" in html


@pytest.mark.unit
def test_table_to_html_empty_table_produces_minimal_html():
    empty = {"row_count": 0, "column_count": 0, "cells": []}
    html = table_to_html(empty)
    assert html == "<table></table>"


# ---------------------------------------------------------------------------
# Tests: _table_to_plain_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_table_to_plain_text_all_cells_present():
    plain = _table_to_plain_text(_TABLE)
    assert "Battery" in plain
    assert "M1287099-003" in plain
    assert "Part" in plain


@pytest.mark.unit
def test_table_to_plain_text_tab_delimited():
    plain = _table_to_plain_text(_TABLE)
    # At least one tab in output
    assert "\t" in plain


# ---------------------------------------------------------------------------
# Tests: _table_to_embedding_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_table_to_embedding_text_has_table_prefix():
    emb = _table_to_embedding_text(_TABLE)
    assert emb.startswith("Table: ")


@pytest.mark.unit
def test_table_to_embedding_text_has_header_columns():
    emb = _table_to_embedding_text(_TABLE)
    assert "Part" in emb
    assert "Part Number" in emb
    assert "Quantity" in emb


@pytest.mark.unit
def test_table_to_embedding_text_has_data_rows():
    emb = _table_to_embedding_text(_TABLE)
    assert "Battery" in emb
    assert "M1287099-003" in emb


# ---------------------------------------------------------------------------
# Tests: _get_table_page_number
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_table_page_number_extracts_from_first_cell():
    page = _get_table_page_number(_TABLE)
    assert page == 2  # _CELLS all on page 2


@pytest.mark.unit
def test_get_table_page_number_returns_none_for_no_cells():
    result = _get_table_page_number({"row_count": 0, "column_count": 0, "cells": []})
    assert result is None


# ---------------------------------------------------------------------------
# Tests: extract_tables
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_tables_returns_dataclass():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert isinstance(result, DocIntelTableResult)


@pytest.mark.unit
def test_extract_tables_yields_one_document_element():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert len(result.document_elements) == 1


@pytest.mark.unit
def test_extract_tables_document_element_type_is_table():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    elem = result.document_elements[0]
    assert elem.element_type == "table"


@pytest.mark.unit
def test_extract_tables_document_element_has_content_html():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    elem = result.document_elements[0]
    assert elem.content_html is not None
    assert "<table>" in elem.content_html
    assert "<th>Part</th>" in elem.content_html


@pytest.mark.unit
def test_extract_tables_document_element_source_file_id():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert result.document_elements[0].source_file_id == _SOURCE_FILE_ID


@pytest.mark.unit
def test_extract_tables_document_element_blob_url_is_none():
    """blob_url is left None — uploader sets it after artifact upload."""
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert result.document_elements[0].blob_url is None


@pytest.mark.unit
def test_extract_tables_document_element_page_number():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert result.document_elements[0].page_number == 2


@pytest.mark.unit
def test_extract_tables_yields_one_chunk():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert len(result.chunks) == 1


@pytest.mark.unit
def test_extract_tables_chunk_type_is_table_html():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert result.chunks[0].chunk_type == "table_html"


@pytest.mark.unit
def test_extract_tables_chunk_has_content_html():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    chunk = result.chunks[0]
    assert chunk.content_html is not None
    assert "<table>" in chunk.content_html


@pytest.mark.unit
def test_extract_tables_chunk_has_embedding_text():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert result.chunks[0].embedding_text is not None


@pytest.mark.unit
def test_extract_tables_chunk_document_element_id_fk():
    """Chunk's document_element_id must point to the corresponding element."""
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert result.chunks[0].document_element_id == result.document_elements[0].document_element_id


@pytest.mark.unit
def test_extract_tables_html_artifacts_keyed_correctly():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert "table_0.html" in result.html_artifacts
    assert "<table>" in result.html_artifacts["table_0.html"]


@pytest.mark.unit
def test_extract_tables_markdown_returned():
    content = "# Parts List\n\nSome markdown."
    di_result = _make_di_result(content=content)
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert result.markdown == content


@pytest.mark.unit
def test_extract_tables_section_path_propagated():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, section_path="§3 Components", now=_NOW)
    assert result.document_elements[0].section_path == "§3 Components"
    assert result.chunks[0].section_path == "§3 Components"


@pytest.mark.unit
def test_extract_tables_empty_tables_list():
    di_result = _make_di_result(tables=[])
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert result.document_elements == []
    assert result.chunks == []
    assert result.html_artifacts == {}


@pytest.mark.unit
def test_extract_tables_multiple_tables():
    di_result = _make_di_result(tables=[_TABLE, _TABLE])
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert len(result.document_elements) == 2
    assert len(result.chunks) == 2
    assert "table_0.html" in result.html_artifacts
    assert "table_1.html" in result.html_artifacts


# ---------------------------------------------------------------------------
# Tests: deterministic IDs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_tables_document_element_id_is_stable():
    """Same inputs must produce the same document_element_id."""
    di_result = _make_di_result()
    r1 = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    r2 = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert r1.document_elements[0].document_element_id == r2.document_elements[0].document_element_id


@pytest.mark.unit
def test_extract_tables_chunk_id_is_stable():
    """Same inputs must produce the same chunk_id."""
    di_result = _make_di_result()
    r1 = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    r2 = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    assert r1.chunks[0].chunk_id == r2.chunks[0].chunk_id


@pytest.mark.unit
def test_extract_tables_ids_match_manual_computation():
    """Verify IDs are exactly what make_document_element_id / make_chunk_id produce."""
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)

    html = table_to_html(_TABLE)
    html_hash = content_hash(html)
    page_num = _get_table_page_number(_TABLE)
    expected_elem_id = make_document_element_id(_SOURCE_FILE_ID, "table", page_num, 0, html_hash)
    expected_chunk_id = make_chunk_id(_SOURCE_FILE_ID, "table_html", html_hash)

    assert result.document_elements[0].document_element_id == expected_elem_id
    assert result.chunks[0].chunk_id == expected_chunk_id


@pytest.mark.unit
def test_extract_tables_content_hash_matches_html():
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    elem = result.document_elements[0]
    expected_hash = content_hash(elem.content_html)
    assert elem.content_hash == expected_hash


# ---------------------------------------------------------------------------
# Tests: write_table_artifacts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_table_artifacts_creates_html_file(tmp_path):
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    written = write_table_artifacts(result.html_artifacts, tmp_path, _SOURCE_FILE_ID)

    safe_id = _SOURCE_FILE_ID.replace(":", "_")
    html_path = tmp_path / "tables" / safe_id / "table_0.html"
    assert html_path in written
    assert html_path.exists()


@pytest.mark.unit
def test_write_table_artifacts_html_file_content(tmp_path):
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    write_table_artifacts(result.html_artifacts, tmp_path, _SOURCE_FILE_ID)

    safe_id = _SOURCE_FILE_ID.replace(":", "_")
    html_path = tmp_path / "tables" / safe_id / "table_0.html"
    content_read = html_path.read_text(encoding="utf-8")
    assert "<table>" in content_read
    assert "<th>Part</th>" in content_read
    assert "<td>Battery</td>" in content_read


@pytest.mark.unit
def test_write_table_artifacts_creates_md_companion(tmp_path):
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    written = write_table_artifacts(result.html_artifacts, tmp_path, _SOURCE_FILE_ID)

    safe_id = _SOURCE_FILE_ID.replace(":", "_")
    md_path = tmp_path / "tables" / safe_id / "table_0.md"
    assert md_path in written
    assert md_path.exists()


@pytest.mark.unit
def test_write_table_artifacts_returns_both_paths(tmp_path):
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    written = write_table_artifacts(result.html_artifacts, tmp_path, _SOURCE_FILE_ID)
    # 1 table → 2 files (.html + .md)
    assert len(written) == 2


@pytest.mark.unit
def test_write_table_artifacts_creates_subdirectory(tmp_path):
    di_result = _make_di_result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    write_table_artifacts(result.html_artifacts, tmp_path, _SOURCE_FILE_ID)

    safe_id = _SOURCE_FILE_ID.replace(":", "_")
    table_dir = tmp_path / "tables" / safe_id
    assert table_dir.is_dir()


@pytest.mark.unit
def test_write_table_artifacts_empty_artifacts(tmp_path):
    written = write_table_artifacts({}, tmp_path, _SOURCE_FILE_ID)
    assert written == []


# ---------------------------------------------------------------------------
# Tests: get_document_markdown
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_document_markdown_returns_content():
    di_result = _make_di_result(content="# Heading\n\nSome text.")
    md = get_document_markdown(di_result)
    assert md == "# Heading\n\nSome text."


@pytest.mark.unit
def test_get_document_markdown_returns_empty_string_for_none():
    mock = MagicMock()
    mock.content = None
    assert get_document_markdown(mock) == ""


# ---------------------------------------------------------------------------
# Tests: DocIntelClient.analyze_document_bytes — markdown output option
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_analyze_document_bytes_passes_output_content_format():
    mock_di = MagicMock()
    poller = MagicMock()
    result_mock = MagicMock()
    result_mock.pages = []
    result_mock.paragraphs = []
    result_mock.content = "# Doc\n\nSome text."
    poller.result.return_value = result_mock
    mock_di.begin_analyze_document.return_value = poller

    client = DocIntelClient(_DI_CONFIG, _di_client=mock_di)
    client.analyze_document_bytes(b"data", "img:test", output_content_format="markdown", now=_NOW)

    call_kwargs = mock_di.begin_analyze_document.call_args.kwargs
    assert call_kwargs.get("output_content_format") == "markdown"


@pytest.mark.unit
def test_analyze_document_bytes_no_format_omits_kwarg():
    """When output_content_format is None, the kwarg must not be forwarded."""
    mock_di = MagicMock()
    poller = MagicMock()
    result_mock = MagicMock()
    result_mock.pages = []
    result_mock.paragraphs = []
    result_mock.content = ""
    poller.result.return_value = result_mock
    mock_di.begin_analyze_document.return_value = poller

    client = DocIntelClient(_DI_CONFIG, _di_client=mock_di)
    client.analyze_document_bytes(b"data", "img:test", now=_NOW)

    call_kwargs = mock_di.begin_analyze_document.call_args.kwargs
    assert "output_content_format" not in call_kwargs


@pytest.mark.unit
def test_analyze_document_url_passes_output_content_format():
    mock_di = MagicMock()
    poller = MagicMock()
    result_mock = MagicMock()
    result_mock.pages = []
    result_mock.paragraphs = []
    result_mock.content = ""
    poller.result.return_value = result_mock
    mock_di.begin_analyze_document.return_value = poller

    client = DocIntelClient(_DI_CONFIG, _di_client=mock_di)
    client.analyze_document_url("https://fake.blob/doc.pdf", "img:test",
                                 output_content_format="markdown", now=_NOW)

    call_kwargs = mock_di.begin_analyze_document.call_args.kwargs
    assert call_kwargs.get("output_content_format") == "markdown"


# ---------------------------------------------------------------------------
# Integration-style: use conftest fixture (analyze_result_tables.json)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_tables_with_conftest_fixture():
    """Use the shared tables fixture JSON via conftest factory."""
    mock_client = make_document_intelligence_client_with_tables()
    di_result = mock_client.begin_analyze_document().result()

    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)

    assert len(result.document_elements) == 1
    elem = result.document_elements[0]
    assert elem.element_type == "table"
    assert elem.content_html is not None
    assert "<th>Part</th>" in elem.content_html
    assert "<td>Battery</td>" in elem.content_html
    assert "<td>M1287099-003</td>" in elem.content_html

    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.chunk_type == "table_html"
    assert chunk.document_element_id == elem.document_element_id


@pytest.mark.unit
def test_extract_tables_conftest_fixture_page_number():
    mock_client = make_document_intelligence_client_with_tables()
    di_result = mock_client.begin_analyze_document().result()
    result = extract_tables(di_result, _SOURCE_FILE_ID, now=_NOW)
    # Fixture cells are on page 1
    assert result.document_elements[0].page_number == 1
