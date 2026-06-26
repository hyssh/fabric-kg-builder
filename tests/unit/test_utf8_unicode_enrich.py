"""Tests for the three Surface PDF live-run bugs fixed 2026-06-24.

Bug 1 — UnicodeEncodeError on Windows cp1252 console
    The success echo ``[enrich] enriched {name} → {out_dir}`` contains → (U+2192)
    which cp1252 cannot encode.  cli/main.py now reconfigures stdout/stderr to
    UTF-8 with errors='replace' before the CLI runs.

Bug 2 — entities=0 / relationships=0 in canonical JSON
    enrich_documents now batches by section_path, aggregates entities and
    relationships from every section batch, and wraps each section call in
    try/except so one bad section never aborts the others.

Bug 3 — chunks missing chunk_type/content abort the pass
    Chunk.chunk_type and Chunk.content are now Optional.  canonicalize drops
    chunks with missing content (warning logged) instead of failing pydantic
    validation for the whole pass.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.orchestrator import (
    CanonicalRecords,
    canonicalize_llm_output,
    enrich_documents,
)
from fabric_kg_builder.enrichment.output_schema import validate
from fabric_kg_builder.model.schemas import DocumentElementRow

# ---------------------------------------------------------------------------
# Constants / shared fixtures
# ---------------------------------------------------------------------------

_DUMMY_CONFIG = FoundryConfig(endpoint="https://test.endpoint/")
_SOURCE_FILE_ID = "src:surface-pro-7-service-abc"
_NOW = datetime(2026, 6, 24, 16, 0, 0, tzinfo=timezone.utc)

_VALID_LLM_OUTPUT = {
    "source_file_id": _SOURCE_FILE_ID,
    "pass": "p2",
    "entities": [
        {
            "id_hint": "device:surface-pro-7",
            "type": "Device",
            "label": "Surface Pro 7",
            "aliases": [],
            "confidence": 0.97,
        },
        {
            "id_hint": "part:1796",
            "type": "PartNumber",
            "label": "M1078441",
            "aliases": [],
            "confidence": 0.93,
        },
    ],
    "relationships": [
        {
            "id_hint": "rel:part-of",
            "source_id_hint": "part:1796",
            "relation": "part_of",
            "target_id_hint": "device:surface-pro-7",
            "confidence": 0.88,
        }
    ],
    "chunks": [
        {
            "id_hint": "chunk:intro",
            "chunk_type": "section_text",
            "content": "Surface Pro 7 battery replacement procedure.",
        }
    ],
    "visual_assets": [],
    "visual_regions": [],
    "evidence": [
        {
            "id_hint": "ev:1",
            "source_type": "document_span",
            "page_number": 1,
            "text": "Surface Pro 7 part M1078441.",
        }
    ],
    "placeholder_suggestions": [],
}

# Minimal valid PDF bytes (single page, "Surface Pro 7" text).
_MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
    b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"5 0 obj<</Length 44>>\nstream\n"
    b"BT /F1 12 Tf 100 700 Td (Surface Pro 7) Tj ET\n"
    b"endstream\nendobj\n"
    b"xref\n0 6\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000058 00000 n \n"
    b"0000000115 00000 n \n"
    b"0000000266 00000 n \n"
    b"0000000342 00000 n \n"
    b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n436\n%%EOF"
)


def _make_client(fixture_json: dict) -> FoundryClient:
    content_str = json.dumps(fixture_json)
    mock_sdk = MagicMock()
    completion = MagicMock(
        choices=[MagicMock(message=MagicMock(content=content_str))]
    )
    mock_sdk.chat.completions.create.return_value = completion
    return FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)


def _make_element(
    *,
    elem_id: str,
    element_type: str = "paragraph",
    content: str | None,
    section_path: str | None = None,
    page_number: int = 1,
    sort_order: int = 0,
) -> DocumentElementRow:
    now = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
    return DocumentElementRow(
        document_element_id=elem_id,
        source_file_id=_SOURCE_FILE_ID,
        element_type=element_type,
        content=content,
        section_path=section_path,
        page_number=page_number,
        sort_order=sort_order,
        content_hash=f"hash-{elem_id}",
        extracted_at=now,
    )


# ---------------------------------------------------------------------------
# Bug 1 — UTF-8 console reconfiguration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUtf8ConsoleReconfiguration:
    """_configure_utf8_console() must reconfigure stdout/stderr on Windows."""

    def test_reconfigure_called_on_win32(self):
        """On win32, reconfigure(encoding='utf-8', errors='replace') is called."""
        from fabric_kg_builder.cli.main import _configure_utf8_console

        mock_stream = MagicMock()
        mock_stream.reconfigure = MagicMock()

        with patch("sys.platform", "win32"), \
             patch("sys.stdout", mock_stream), \
             patch("sys.stderr", mock_stream):
            _configure_utf8_console()

        mock_stream.reconfigure.assert_called_with(
            encoding="utf-8", errors="replace"
        )

    def test_no_reconfigure_on_non_windows(self):
        """On non-Windows platforms the streams are left untouched."""
        from fabric_kg_builder.cli.main import _configure_utf8_console

        mock_stream = MagicMock()
        mock_stream.reconfigure = MagicMock()

        with patch("sys.platform", "linux"), \
             patch("sys.stdout", mock_stream), \
             patch("sys.stderr", mock_stream):
            _configure_utf8_console()

        mock_stream.reconfigure.assert_not_called()

    def test_reconfigure_silences_exception(self):
        """A stream that raises from reconfigure() must not crash the CLI."""
        from fabric_kg_builder.cli.main import _configure_utf8_console

        broken_stream = MagicMock()
        broken_stream.reconfigure.side_effect = IOError("cannot reconfigure")

        with patch("sys.platform", "win32"), \
             patch("sys.stdout", broken_stream), \
             patch("sys.stderr", broken_stream):
            _configure_utf8_console()  # must not raise

    def test_stream_without_reconfigure_is_safe(self):
        """A stream without reconfigure() (e.g. raw BytesIO) must be skipped."""
        from fabric_kg_builder.cli.main import _configure_utf8_console
        import io

        raw = io.BytesIO()  # no reconfigure attribute

        with patch("sys.platform", "win32"), \
             patch("sys.stdout", raw), \
             patch("sys.stderr", raw):
            _configure_utf8_console()  # must not raise

    def test_enrich_arrow_echo_does_not_crash(self, tmp_path: Path):
        """Enrich success echo containing → must produce exit 0, not exit 4.

        On Windows without the UTF-8 fix, click.echo('… → …') raises
        UnicodeEncodeError which is caught by enrich_cmd's per-file
        try/except, increments errors, and returns exit 4.  With the fix the
        echo succeeds and exit is 0.
        """
        from click.testing import CliRunner
        from fabric_kg_builder.cli import cli

        pdf_path = tmp_path / "surface_pro7.pdf"
        pdf_path.write_bytes(_MINIMAL_PDF)
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)

        client = _make_client(_VALID_LLM_OUTPUT)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={"_foundry_client": client},
        )

        assert result.exception is None, (
            f"CLI raised {result.exception!r}\nOutput: {result.output}"
        )
        assert result.exit_code == 0, (
            f"Expected exit 0 (got {result.exit_code}). "
            f"UnicodeEncodeError or other error crashed the enrich echo.\n"
            f"Output: {result.output}"
        )
        # Confirm the → is present in the output (not swallowed).
        assert "\u2192" in result.output, (
            "Expected → in enrich success echo; not found in output:\n"
            + result.output
        )


# ---------------------------------------------------------------------------
# Bug 2 — entity/relationship capture across sections (multi-batch)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMultiSectionEntityCapture:
    """enrich_documents batches by section and aggregates entities + rels."""

    def test_multi_section_entities_and_relationships(self, tmp_path: Path):
        """Two sections each returning entities → output has both sets."""
        call_count = [0]

        def side_effect(**kwargs):
            call_count[0] += 1
            # Both sections return the same valid payload (2 entities each).
            return MagicMock(
                choices=[MagicMock(message=MagicMock(content=json.dumps(_VALID_LLM_OUTPUT)))]
            )

        mock_sdk = MagicMock()
        mock_sdk.chat.completions.create.side_effect = side_effect
        client = FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)

        elements = [
            _make_element(elem_id="a1", content="Surface Pro 7 overview.", section_path="Overview"),
            _make_element(elem_id="b1", content="Battery replacement steps.", section_path="Battery"),
        ]

        records = enrich_documents(
            document_elements=elements,
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=tmp_path / "enriched",
        )

        # Two sections → two LLM calls.
        assert call_count[0] == 2
        # Each call returns 2 entities; dedup by canonical key means 2 unique.
        assert len(records.entities) > 0
        assert len(records.relationships) > 0

    def test_bad_section_does_not_abort_other_sections(self, tmp_path: Path):
        """One section failing (RuntimeError) must not prevent other sections."""
        call_count = [0]

        def side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First section (alphabetical): valid output.
                return MagicMock(
                    choices=[MagicMock(message=MagicMock(
                        content=json.dumps(_VALID_LLM_OUTPUT)
                    ))]
                )
            # Second section: simulate LLM call crash.
            raise RuntimeError("simulated LLM failure for section 2")

        mock_sdk = MagicMock()
        mock_sdk.chat.completions.create.side_effect = side_effect
        client = FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)

        # Use alphabetically ordered section paths so the first call is
        # deterministic regardless of defaultdict insertion order.
        elements = [
            _make_element(elem_id="a1", content="Surface Pro 7 specs.", section_path="A-Overview"),
            _make_element(elem_id="b1", content="Accessories list.", section_path="B-Accessories"),
        ]

        records = enrich_documents(
            document_elements=elements,
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=tmp_path / "enriched",
        )

        # Despite one section failing, entities from the good section survive.
        assert len(records.entities) > 0, (
            "Expected entities from the successful section; got 0. "
            "A section failure must not discard other sections' entities."
        )
        assert len(records.relationships) > 0

    def test_section_checkpoint_keys_written(self, tmp_path: Path):
        """After enrich_documents, checkpoint contains both section and doc keys."""
        client = _make_client(_VALID_LLM_OUTPUT)
        out_dir = tmp_path / "enriched"

        elements = [
            _make_element(elem_id="x1", content="Text for section X.", section_path="SecX"),
        ]
        enrich_documents(
            document_elements=elements,
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=out_dir,
        )

        checkpoint = out_dir / ".checkpoint.json"
        assert checkpoint.exists()
        data = json.loads(checkpoint.read_text())
        completed = set(data["completed"])

        # Section-level key.
        section_key = f"{_SOURCE_FILE_ID}:section:SecX"
        assert section_key in completed, (
            f"Expected section key '{section_key}' in checkpoint: {completed}"
        )
        # Document-level key for future document-level resume.
        assert _SOURCE_FILE_ID in completed, (
            f"Expected doc key '{_SOURCE_FILE_ID}' in checkpoint: {completed}"
        )

    def test_document_level_resume_skips_whole_doc(self, tmp_path: Path):
        """With source_file_id in checkpoint, resume=True skips all LLM calls."""
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)
        checkpoint = out_dir / ".checkpoint.json"
        checkpoint.write_text(
            json.dumps({"completed": [_SOURCE_FILE_ID]}),
            encoding="utf-8",
        )

        mock_sdk = MagicMock()
        mock_sdk.chat.completions.create.side_effect = AssertionError(
            "LLM must not be called when document is already in checkpoint"
        )
        client = FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)

        result = enrich_documents(
            document_elements=[
                _make_element(elem_id="r1", content="Any content.", section_path="S1")
            ],
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=out_dir,
            resume=True,
        )
        assert isinstance(result, CanonicalRecords)
        assert result.entities == []


# ---------------------------------------------------------------------------
# Bug 3 — chunks missing chunk_type / content are dropped, not fatal
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChunkLeniency:
    """LLM chunks missing chunk_type or content are silently dropped."""

    def test_chunk_missing_content_is_dropped(self):
        """A chunk with content=None must be dropped; entities unaffected."""
        payload = {
            "source_file_id": _SOURCE_FILE_ID,
            "pass": "p2",
            "entities": [
                {
                    "id_hint": "device:surface-pro-7",
                    "type": "Device",
                    "label": "Surface Pro 7",
                    "aliases": [],
                    "confidence": 0.95,
                }
            ],
            "relationships": [],
            "chunks": [
                # Valid chunk.
                {"chunk_type": "section_text", "content": "Surface Pro 7 overview."},
                # Missing content — must be dropped, not abort.
                {"chunk_type": "section_text", "content": None},
                # Missing both — must also be dropped.
                {},
            ],
            "visual_assets": [],
            "visual_regions": [],
            "evidence": [],
            "placeholder_suggestions": [],
        }

        # Pydantic must accept this (not raise).
        output = validate(payload)

        # canonicalize must keep only the chunk with real content.
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        assert len(records.chunks) == 1
        assert records.chunks[0].content == "Surface Pro 7 overview."

    def test_entities_survive_when_all_chunks_malformed(self):
        """Entities must be captured even if ALL LLM chunks are malformed."""
        payload = {
            "source_file_id": _SOURCE_FILE_ID,
            "pass": "p2",
            "entities": [
                {
                    "id_hint": "device:surface-pro-7",
                    "type": "Device",
                    "label": "Surface Pro 7",
                    "aliases": [],
                    "confidence": 0.95,
                },
                {
                    "id_hint": "part:m1078441",
                    "type": "PartNumber",
                    "label": "M1078441",
                    "aliases": [],
                    "confidence": 0.90,
                },
            ],
            "relationships": [
                {
                    "id_hint": "rel:part-of",
                    "source_id_hint": "part:m1078441",
                    "relation": "part_of",
                    "target_id_hint": "device:surface-pro-7",
                    "confidence": 0.88,
                }
            ],
            # All chunks lack content.
            "chunks": [
                {"chunk_type": "section_text"},
                {"content": None},
                {},
            ],
            "visual_assets": [],
            "visual_regions": [],
            "evidence": [],
            "placeholder_suggestions": [],
        }

        output = validate(payload)
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)

        assert len(records.entities) == 2, (
            f"Expected 2 entities even with all chunks malformed; got {len(records.entities)}"
        )
        assert len(records.relationships) == 1
        assert len(records.chunks) == 0  # all dropped

    def test_enrich_batch_captures_entities_when_chunks_malformed(self, tmp_path: Path):
        """End-to-end: LLM returns malformed chunks → entities still in output."""
        payload_with_bad_chunks = {
            "source_file_id": _SOURCE_FILE_ID,
            "pass": "p2",
            "entities": [
                {
                    "id_hint": "device:surface-pro-7",
                    "type": "Device",
                    "label": "Surface Pro 7",
                    "aliases": [],
                    "confidence": 0.95,
                }
            ],
            "relationships": [],
            # Chunks missing chunk_type and content — formerly caused
            # pydantic ValidationError that skipped the whole pass.
            "chunks": [
                {"chunk_type": None, "content": None},
                {"id_hint": "c:bad"},
            ],
            "visual_assets": [],
            "visual_regions": [],
            "evidence": [],
            "placeholder_suggestions": [],
        }

        from fabric_kg_builder.enrichment.orchestrator import enrich_batch

        client = _make_client(payload_with_bad_chunks)
        records = enrich_batch(
            source_content="Surface Pro 7 service manual.",
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=tmp_path / "enriched",
            default_source_type="document_span",
        )

        assert len(records.entities) == 1, (
            f"Expected 1 entity despite malformed chunks; got {len(records.entities)}"
        )
        assert records.entities[0].display_name == "Surface Pro 7"
        assert len(records.chunks) == 0
