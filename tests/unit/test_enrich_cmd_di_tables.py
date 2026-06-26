"""Unit tests: Document Intelligence table wiring into the enrich document path.

Verifies (coordinator-tables-via-docintel.md, 2026-06-24):
1. With a mock DI client returning a table, the document enrich path produces a
   table_html chunk + table document_element in the canonical output.
2. The LLM system prompt explicitly instructs no table transcription (no table_row
   / table_cell emission).
3. An LLM-emitted chunk_type='table_row' is dropped by canonicalize_llm_output.
4. When DI is NOT configured (di_layout_client=None), the pipeline completes
   successfully with exit 0 — no crash.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.config.schema import DocumentIntelligenceConfig, FoundryConfig
from fabric_kg_builder.enrichment.docintel import DocIntelClient
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.orchestrator import (
    _ENRICH_SYSTEM_PROMPT,
    CanonicalRecords,
    canonicalize_llm_output,
)
from fabric_kg_builder.enrichment.output_schema import LLMOutput
from tests.conftest import make_document_intelligence_client_with_tables

# ---------------------------------------------------------------------------
# Minimal PDF bytes (one page, "Hello World")
# ---------------------------------------------------------------------------

_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj
4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
5 0 obj<</Length 44>>
stream
BT /F1 12 Tf 100 700 Td (Hello World) Tj ET
endstream
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000266 00000 n 
0000000342 00000 n 
trailer<</Size 6/Root 1 0 R>>
startxref
436
%%EOF"""

_DUMMY_FOUNDRY_CFG = FoundryConfig(endpoint="https://test.endpoint/")
_DUMMY_DI_CFG = DocumentIntelligenceConfig(
    endpoint="https://fake-di.cognitiveservices.azure.com/"
)

_MOCK_LLM_OUTPUT = {
    "source_file_id": "src:test",
    "pass": "p2",
    "entities": [
        {"id_hint": "device:surface-pro", "type": "Device", "label": "Surface Pro",
         "aliases": [], "confidence": 0.95}
    ],
    "relationships": [],
    "chunks": [
        {"id_hint": "chunk:intro", "chunk_type": "section_text",
         "content": "Hello World is a Surface device reference."}
    ],
    "visual_assets": [],
    "visual_regions": [],
    "evidence": [
        {"id_hint": "ev:1", "source_type": "document_span", "page_number": 1,
         "text": "Hello World"}
    ],
    "placeholder_suggestions": [],
}


def _make_foundry_client(fixture_json: dict) -> FoundryClient:
    content_str = json.dumps(fixture_json)
    mock_sdk = MagicMock()
    completion = MagicMock(choices=[MagicMock(message=MagicMock(content=content_str))])
    mock_sdk.chat.completions.create.return_value = completion
    return FoundryClient(_DUMMY_FOUNDRY_CFG, _sdk_client=mock_sdk)


def _make_di_layout_client() -> DocIntelClient:
    """DocIntelClient wrapping the tables fixture mock."""
    raw_mock = make_document_intelligence_client_with_tables()
    return DocIntelClient(_DUMMY_DI_CFG, _di_client=raw_mock)


# ---------------------------------------------------------------------------
# Test 1 — System prompt instructs no table transcription
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_system_prompt_forbids_table_row_transcription() -> None:
    """_ENRICH_SYSTEM_PROMPT must explicitly tell the LLM not to emit table_row chunks."""
    assert "table_row" in _ENRICH_SYSTEM_PROMPT, (
        "System prompt must mention 'table_row' to forbid its transcription"
    )
    assert "Document Intelligence" in _ENRICH_SYSTEM_PROMPT, (
        "System prompt must reference Document Intelligence as the table source of truth"
    )


# ---------------------------------------------------------------------------
# Test 2 — canonicalize drops LLM-emitted table_row chunk
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_canonicalize_drops_llm_table_row_chunk(tmp_path: Path) -> None:
    """LLM-emitted chunk_type='table_row' must be silently dropped by canonicalize."""
    payload = {
        "source_file_id": "src:tbl-drop-test",
        "pass": "p2",
        "entities": [],
        "relationships": [],
        "chunks": [
            # legitimate chunk — must survive
            {"id_hint": "chunk:good", "chunk_type": "section_text",
             "content": "Good section content."},
            # LLM-transcribed table row — must be dropped
            {"id_hint": "chunk:bad-row", "chunk_type": "table_row",
             "content": "Battery | M1287099-003 | 1"},
        ],
        "visual_assets": [],
        "visual_regions": [],
        "evidence": [],
        "placeholder_suggestions": [],
    }
    output = LLMOutput.model_validate(payload)
    records = canonicalize_llm_output(output, "src:tbl-drop-test")

    chunk_types = [c.chunk_type for c in records.chunks]
    assert "table_row" not in chunk_types, (
        "canonicalize must drop LLM-emitted table_row chunks"
    )
    assert "section_text" in chunk_types, (
        "canonicalize must keep legitimate section_text chunks"
    )
    assert len(records.chunks) == 1


# ---------------------------------------------------------------------------
# Test 3 — DI tables wired into enrich path (mock DI returning one table)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_di_table_wire_produces_table_html_chunk(tmp_path: Path) -> None:
    """With a mock DI client, canonical JSON must contain a table_html chunk."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client(_MOCK_LLM_OUTPUT)
    di_layout_client = _make_di_layout_client()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
        obj={
            "_foundry_client": foundry_client,
            "_di_layout_client": di_layout_client,
        },
    )
    assert result.exit_code == 0, (
        f"enrich exited {result.exit_code}.\n"
        f"Output: {result.output}\nException: {result.exception}"
    )

    canonical_files = list(out_dir.glob("*_canonical.json"))
    assert canonical_files, "No _canonical.json written"

    data = json.loads(canonical_files[0].read_text())
    chunk_types = [c.get("chunk_type") for c in data.get("chunks", [])]
    assert "table_html" in chunk_types, (
        f"Expected a table_html chunk from DI, got chunk_types={chunk_types}"
    )


@pytest.mark.unit
def test_di_table_wire_produces_table_document_element(tmp_path: Path) -> None:
    """With a mock DI client, canonical JSON must contain a table document_element."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client(_MOCK_LLM_OUTPUT)
    di_layout_client = _make_di_layout_client()

    runner = CliRunner()
    runner.invoke(
        cli,
        ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
        obj={
            "_foundry_client": foundry_client,
            "_di_layout_client": di_layout_client,
        },
    )

    canonical_files = list(out_dir.glob("*_canonical.json"))
    assert canonical_files
    data = json.loads(canonical_files[0].read_text())
    elem_types = [e.get("element_type") for e in data.get("document_elements", [])]
    assert "table" in elem_types, (
        f"Expected a table document_element from DI, got element_types={elem_types}"
    )


@pytest.mark.unit
def test_di_table_chunk_has_content_html(tmp_path: Path) -> None:
    """DI-produced table_html chunk must carry a non-empty content_html field."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client(_MOCK_LLM_OUTPUT)
    di_layout_client = _make_di_layout_client()

    runner = CliRunner()
    runner.invoke(
        cli,
        ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
        obj={
            "_foundry_client": foundry_client,
            "_di_layout_client": di_layout_client,
        },
    )

    data = json.loads(list(out_dir.glob("*_canonical.json"))[0].read_text())
    table_chunks = [c for c in data.get("chunks", []) if c.get("chunk_type") == "table_html"]
    assert table_chunks, "No table_html chunks found"
    for tc in table_chunks:
        assert tc.get("content_html"), (
            "table_html chunk must have content_html set"
        )
        assert "<table>" in tc["content_html"], (
            "content_html must contain an HTML table"
        )


# ---------------------------------------------------------------------------
# Test 4 — DI not configured: pipeline still works (no crash, exit 0)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_di_not_configured_pipeline_still_works(tmp_path: Path) -> None:
    """When di_layout_client is None (DI not configured), enrich exits 0 without crashing."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client(_MOCK_LLM_OUTPUT)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
        obj={
            "_foundry_client": foundry_client,
            # _di_layout_client intentionally absent → DI not configured
        },
    )
    assert result.exit_code == 0, (
        f"enrich should exit 0 without DI. "
        f"Exit: {result.exit_code}\nOutput: {result.output}\nException: {result.exception}"
    )

    canonical_files = list(out_dir.glob("*_canonical.json"))
    assert canonical_files, "Canonical JSON must still be written without DI"
    data = json.loads(canonical_files[0].read_text())
    # No table_html chunks expected when DI is absent
    chunk_types = [c.get("chunk_type") for c in data.get("chunks", [])]
    assert "table_html" not in chunk_types
