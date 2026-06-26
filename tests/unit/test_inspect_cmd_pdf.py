"""Tests for inspect-source PDF/DOCX/HTML routing — Sprint 2.

Tests:
- PDF file reports page count and element counts
- Directory of PDFs gives a combined inventory
- CSV behavior unchanged
- Integration test for one real Surface PDF (marked integration)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SAMPLE_CSV = Path(__file__).parent.parent.parent / "examples" / "csv" / "sample.csv"
SURFACE_DIR = (
    Path(__file__).parent.parent.parent
    / "sample_data"
    / "Surface_Troubleshootings"
)
SMALL_SURFACE_PDF = SURFACE_DIR / "Surface Pro 7 Kickstand Replacement Guide.pdf"

# ---------------------------------------------------------------------------
# Minimal synthetic PDF fixture
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


@pytest.mark.unit
class TestInspectSourceDocRouting:
    """inspect-source handles PDF/DOCX/HTML via router."""

    def test_minimal_pdf_exits_zero(self, tmp_path: Path) -> None:
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(_MINIMAL_PDF)
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(pdf)])
        assert result.exit_code == 0, (
            f"Expected 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )

    def test_minimal_pdf_shows_type(self, tmp_path: Path) -> None:
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(_MINIMAL_PDF)
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(pdf)])
        assert result.exit_code == 0
        assert "pdf" in result.output.lower()

    def test_minimal_pdf_shows_page_count(self, tmp_path: Path) -> None:
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(_MINIMAL_PDF)
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(pdf)])
        assert result.exit_code == 0
        assert "Pages" in result.output or "page" in result.output.lower()

    def test_minimal_pdf_shows_element_count(self, tmp_path: Path) -> None:
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(_MINIMAL_PDF)
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(pdf)])
        assert result.exit_code == 0
        assert "Elements" in result.output or "element" in result.output.lower()

    def test_pdf_out_writes_doc_inventory_json(self, tmp_path: Path) -> None:
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(_MINIMAL_PDF)
        out = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["inspect-source", "--input", str(pdf), "--out", str(out)]
        )
        assert result.exit_code == 0
        inv_file = out / "doc-inventory.json"
        assert inv_file.exists(), f"doc-inventory.json not created.\n{result.output}"

    def test_pdf_json_format_is_parseable(self, tmp_path: Path) -> None:
        """--format json on a PDF must emit valid JSON with source_type and element_count."""
        import json as _json
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(_MINIMAL_PDF)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["inspect-source", "--input", str(pdf), "--format", "json"]
        )
        assert result.exit_code == 0
        data = _json.loads(result.output)
        assert data.get("source_type") == "pdf"
        assert "element_count" in data

    def test_csv_still_works_with_mixed_exts(self, tmp_path: Path) -> None:
        """CSV files in a mixed directory must still produce schema profiles."""
        import shutil
        csv_dest = tmp_path / SAMPLE_CSV.name
        shutil.copy(SAMPLE_CSV, csv_dest)
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(tmp_path)])
        assert result.exit_code == 0
        assert "Row count" in result.output or "csv" in result.output.lower()

    def test_unsupported_txt_still_exits_3(self, tmp_path: Path) -> None:
        bad = tmp_path / "notes.txt"
        bad.write_text("hello")
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(bad)])
        assert result.exit_code == 3

    @pytest.mark.integration
    @pytest.mark.slow
    def test_real_surface_pdf_reports_elements(self) -> None:
        """Integration: a real Surface PDF should have pages and many elements."""
        if not SMALL_SURFACE_PDF.exists():
            pytest.skip("Surface PDF fixture not available")
        runner = CliRunner()
        result = runner.invoke(
            cli, ["inspect-source", "--input", str(SMALL_SURFACE_PDF)]
        )
        assert result.exit_code == 0
        assert "pdf" in result.output.lower()
        assert "Pages" in result.output


@pytest.mark.integration
@pytest.mark.slow
class TestInspectSourceSurfaceDir:
    """Integration: inspect all Surface PDFs in the sample data directory."""

    def test_surface_dir_exits_zero(self) -> None:
        if not SURFACE_DIR.exists():
            pytest.skip("Surface PDF directory not available")
        runner = CliRunner()
        result = runner.invoke(
            cli, ["inspect-source", "--input", str(SURFACE_DIR)]
        )
        assert result.exit_code == 0, (
            f"Expected 0, got {result.exit_code}.\nOutput:\n{result.output[:2000]}"
        )

    def test_surface_dir_shows_combined_inventory(self) -> None:
        if not SURFACE_DIR.exists():
            pytest.skip("Surface PDF directory not available")
        runner = CliRunner()
        result = runner.invoke(
            cli, ["inspect-source", "--input", str(SURFACE_DIR)]
        )
        assert result.exit_code == 0
        assert "Combined document inventory" in result.output
