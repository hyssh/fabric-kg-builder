"""Unit tests for the enrich command — PDF/document routing path.

Sprint 2: verify that PDF (and other document) inputs route through the
document pipeline (router → extractor → chunker → enrich_documents) and
produce canonical intermediate JSON including document_elements, chunks,
entities, and evidence.  All tests use mock LLM — no live API calls.

Security assertion: domain text must appear ONLY in the LLM USER message,
never in the system prompt (SPEC-004 §2.3).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.enrichment.foundry_client import FoundryClient

# ---------------------------------------------------------------------------
# Minimal valid PDF (one page, "Hello World" text)
# ---------------------------------------------------------------------------

MINIMAL_PDF = b"""%PDF-1.4
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

_DUMMY_CONFIG = FoundryConfig(endpoint="https://test.endpoint/")

_MOCK_DOMAIN_BRIEF = {
    "domain_brief": "Surface laptop hardware service documentation.",
    "key_entity_types": ["Device", "Component"],
    "key_relationship_types": ["has_component"],
    "extraction_constraints": ["Hardware only."],
    "source_domain_text": "Surface laptop service docs",
}

_MOCK_LLM_OUTPUT = {
    "source_file_id": "src:test",
    "pass": "p2",
    "entities": [
        {
            "id_hint": "device:surface-pro",
            "type": "Device",
            "label": "Surface Pro",
            "aliases": [],
            "confidence": 0.95,
        }
    ],
    "relationships": [],
    "chunks": [
        {
            "id_hint": "chunk:intro",
            "chunk_type": "section_text",
            "content": "Hello World is a Surface device reference.",
        }
    ],
    "visual_assets": [],
    "visual_regions": [],
    "evidence": [
        {
            "id_hint": "ev:1",
            "source_type": "document_span",
            "page_number": 1,
            "text": "Hello World",
        }
    ],
    "placeholder_suggestions": [],
}


def _make_client(fixture_json: dict) -> FoundryClient:
    content_str = json.dumps(fixture_json)
    mock_sdk = MagicMock()
    completion = MagicMock(choices=[MagicMock(message=MagicMock(content=content_str))])
    mock_sdk.chat.completions.create.return_value = completion
    return FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnrichCmdPdf:

    def test_exits_0_with_pdf_input(self, tmp_path: Path) -> None:
        """enrich --input <pdf> exits 0 with a mock LLM client."""
        pdf_path = tmp_path / "tiny.pdf"
        pdf_path.write_bytes(MINIMAL_PDF)
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)
        (out_dir / "domain.json").write_text(json.dumps(_MOCK_DOMAIN_BRIEF), encoding="utf-8")

        mock_client = _make_client(_MOCK_LLM_OUTPUT)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={"_foundry_client": mock_client},
        )
        assert result.exit_code == 0, (
            f"enrich exited {result.exit_code}.\nOutput: {result.output}\n"
            f"Exception: {result.exception}"
        )

    def test_writes_canonical_json_with_all_sections(self, tmp_path: Path) -> None:
        """enrich on PDF writes _canonical.json with document_elements, chunks,
        entities, and evidence keys."""
        pdf_path = tmp_path / "tiny.pdf"
        pdf_path.write_bytes(MINIMAL_PDF)
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)
        (out_dir / "domain.json").write_text(json.dumps(_MOCK_DOMAIN_BRIEF), encoding="utf-8")

        mock_client = _make_client(_MOCK_LLM_OUTPUT)
        runner = CliRunner()
        runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={"_foundry_client": mock_client},
        )

        canonical_files = list(out_dir.glob("*_canonical.json"))
        assert canonical_files, "No _canonical.json written for PDF input"

        data = json.loads(canonical_files[0].read_text())
        assert "source_file_id" in data
        assert "document_elements" in data, "canonical JSON missing document_elements"
        assert "chunks" in data, "canonical JSON missing chunks"
        assert "entities" in data, "canonical JSON missing entities"
        assert "evidence" in data, "canonical JSON missing evidence"

    def test_canonical_json_has_chunks_from_extractor(self, tmp_path: Path) -> None:
        """Structural chunks from Chunker appear in _canonical.json."""
        pdf_path = tmp_path / "tiny.pdf"
        pdf_path.write_bytes(MINIMAL_PDF)
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)
        (out_dir / "domain.json").write_text(json.dumps(_MOCK_DOMAIN_BRIEF), encoding="utf-8")

        mock_client = _make_client(_MOCK_LLM_OUTPUT)
        runner = CliRunner()
        runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={"_foundry_client": mock_client},
        )

        canonical_files = list(out_dir.glob("*_canonical.json"))
        assert canonical_files
        data = json.loads(canonical_files[0].read_text())
        # The PDF has "Hello World" text; the Chunker must produce at least one chunk.
        assert len(data["chunks"]) >= 1, "Expected at least one structural chunk in canonical JSON"

    def test_domain_text_in_user_message_only(self, tmp_path: Path) -> None:
        """Domain text must appear ONLY in the LLM user message, never the system prompt.

        SPEC-004 §2.3 security invariant.
        """
        domain_text = "Surface laptop service docs UNIQUE_DOMAIN_TOKEN"

        pdf_path = tmp_path / "tiny.pdf"
        pdf_path.write_bytes(MINIMAL_PDF)
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)

        # Capture all LLM calls.
        captured_calls: list[dict] = []

        mock_sdk = MagicMock()

        def capture_complete(**kwargs):
            msgs = kwargs.get("messages", [])
            captured_calls.append({"messages": list(msgs)})
            return MagicMock(
                choices=[MagicMock(message=MagicMock(content=json.dumps(_MOCK_LLM_OUTPUT)))]
            )

        mock_sdk.chat.completions.create.side_effect = (
            capture_complete
        )

        # First call returns domain brief, subsequent calls return LLM output.
        domain_brief_json = json.dumps(_MOCK_DOMAIN_BRIEF)
        call_counter = {"i": 0}

        def capture_and_dispatch(**kwargs):
            msgs = kwargs.get("messages", [])
            captured_calls.append({"messages": list(msgs)})
            idx = call_counter["i"]
            call_counter["i"] += 1
            resp_str = domain_brief_json if idx == 0 else json.dumps(_MOCK_LLM_OUTPUT)
            return MagicMock(choices=[MagicMock(message=MagicMock(content=resp_str))])

        mock_sdk.chat.completions.create.side_effect = (
            capture_and_dispatch
        )
        mock_client = FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "enrich",
                "--input", str(pdf_path),
                "--domain-prompt", domain_text,
                "--out", str(out_dir),
            ],
            obj={"_foundry_client": mock_client},
        )
        assert result.exit_code == 0, (
            f"enrich exited {result.exit_code}.\nOutput: {result.output}"
        )

        assert captured_calls, "No LLM calls were made"
        for call in captured_calls:
            for msg in call["messages"]:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "system":
                    assert "UNIQUE_DOMAIN_TOKEN" not in content, (
                        "SECURITY VIOLATION: domain text found in system prompt"
                    )

    def test_csv_path_still_works(self, tmp_path: Path, sample_csv_path: Path) -> None:
        """CSV path is unaffected by the document routing change."""
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)
        (out_dir / "domain.json").write_text(json.dumps(_MOCK_DOMAIN_BRIEF), encoding="utf-8")

        mock_client = _make_client(_MOCK_LLM_OUTPUT)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["enrich", "--input", str(sample_csv_path), "--out", str(out_dir)],
            obj={"_foundry_client": mock_client},
        )
        assert result.exit_code == 0, (
            f"CSV enrich exited {result.exit_code}.\nOutput: {result.output}"
        )

    def test_checkpoint_resume_skips_completed_pdf(self, tmp_path: Path) -> None:
        """Resume skips a PDF that was already enriched (checkpoint present)."""
        pdf_path = tmp_path / "tiny.pdf"
        pdf_path.write_bytes(MINIMAL_PDF)
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)
        (out_dir / "domain.json").write_text(json.dumps(_MOCK_DOMAIN_BRIEF), encoding="utf-8")

        # First run to establish the checkpoint.
        mock_client = _make_client(_MOCK_LLM_OUTPUT)
        runner = CliRunner()
        runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={"_foundry_client": mock_client},
        )

        # Second run with --resume: LLM must NOT be called.
        mock_sdk2 = MagicMock()
        mock_sdk2.chat.completions.create.side_effect = (
            AssertionError("LLM must not be called for already-checkpointed file")
        )
        mock_client2 = FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk2)

        result2 = runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir), "--resume"],
            obj={"_foundry_client": mock_client2},
        )
        assert result2.exit_code == 0, (
            f"Resume enrich failed: {result2.output}"
        )
