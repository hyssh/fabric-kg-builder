"""Extractor routing by source file extension with media-signature validation.

EXT-002: rejects mismatched or unsafe extensions before dispatching.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapter import AdapterError, FailureType
from .csv_loader import load_csv
from .docx_extractor import DocxExtractor
from .html_extractor import HtmlExtractor
from .pdf_extractor import PdfExtractor


# ---------------------------------------------------------------------------
# Extension sets
# ---------------------------------------------------------------------------

_CSV_EXTS = frozenset({".csv", ".tsv", ".xls", ".xlsx"})
_PDF_EXTS = frozenset({".pdf"})
_DOCX_EXTS = frozenset({".docx"})
_HTML_EXTS = frozenset({".html", ".htm", ".md"})
_PPTX_EXTS = frozenset({".pptx"})
_PARQUET_EXTS = frozenset({".parquet"})
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tiff", ".tif"})
_UNSUPPORTED_EXTS = frozenset({".psd"})

_ALL_SUPPORTED = (
    _CSV_EXTS | _PDF_EXTS | _DOCX_EXTS | _HTML_EXTS
    | _PPTX_EXTS | _PARQUET_EXTS | _IMAGE_EXTS
)


def route(path: str | Path) -> str:
    """Return the extractor name for the given file path.

    Performs media-signature validation when the file exists on disk (EXT-002).
    Raises ``AdapterError(UNSUPPORTED)`` for PSD and other explicitly blocked
    types; raises ``AdapterError(MIME_MISMATCH)`` when the file's magic bytes
    contradict the declared extension.

    Returns
    -------
    str
        One of: ``"csv_loader"``, ``"pdf_extractor"``, ``"docx_extractor"``,
        ``"html_extractor"``, ``"pptx_extractor"``, ``"parquet_adapter"``,
        ``"image_adapter"``.
    """
    p = Path(path)
    suffix = p.suffix.lower()

    # Reject explicitly unsupported types regardless of file existence
    if suffix in _UNSUPPORTED_EXTS:
        raise AdapterError(
            FailureType.UNSUPPORTED,
            f"File type '{suffix}' is explicitly unsupported. "
            "(PSD support is pending feasibility evaluation.)",
            source_locator=str(path),
        )

    # Media-signature validation only when file exists
    detected_mime = ""
    if p.exists():
        from .media_type import validate_extension_vs_signature  # noqa: PLC0415
        detected_mime = validate_extension_vs_signature(p)  # raises on mismatch

    if suffix in _CSV_EXTS or detected_mime == "application/vnd.ms-excel":
        return "csv_loader"
    if suffix in _PDF_EXTS:
        return "pdf_extractor"
    if suffix in _DOCX_EXTS:
        return "docx_extractor"
    if suffix in _HTML_EXTS:
        return "html_extractor"
    if suffix in _PPTX_EXTS:
        return "pptx_extractor"
    if suffix in _PARQUET_EXTS:
        return "parquet_adapter"
    if suffix in _IMAGE_EXTS:
        return "image_adapter"

    raise ValueError(f"Unsupported source extension: {suffix or '<none>'}")


def extract(path: str | Path) -> Any:
    """Extract document elements from a file, dispatching by extension.

    Maintains backward-compatible return types for existing formats:
    - CSV/TSV/XLSX → ``CsvLoadResult``
    - PDF          → ``PdfExtractResult``
    - DOCX         → ``DocxExtractResult``
    - HTML/HTM/MD  → ``HtmlExtractResult``

    New formats return ``AdapterResult`` (duck-typing compatible via
    ``.source_file`` and ``.document_elements`` attributes):
    - PPTX         → ``AdapterResult`` from ``PptxExtractor``
    - Parquet      → ``AdapterResult`` from ``ParquetAdapter``
    - PNG/JPG/TIFF → ``AdapterResult`` from ``ImageAdapter``
    """
    extractor = route(path)
    p = Path(path)

    # OOXML archive safety: validate before passing to any Office XML parser.
    # This guards against archive bombs, encrypted entries, and generic ZIPs
    # renamed to Office extensions (EXT-009).
    _OOXML_EXTRACTORS = frozenset({"docx_extractor", "pptx_extractor"})
    if extractor in _OOXML_EXTRACTORS or (
        extractor == "csv_loader"
        and p.suffix.lower() == ".xlsx"
        and detected_mime
        != "application/vnd.ms-excel"
    ):
        if p.exists():
            from .media_type import mime_for_extension, validate_ooxml_archive  # noqa: PLC0415

            declared_mime = mime_for_extension(p.suffix.lower()) or ""
            validate_ooxml_archive(p, declared_mime)

    if extractor == "csv_loader":
        return load_csv(path)
    if extractor == "pdf_extractor":
        return PdfExtractor.extract(path)
    if extractor == "docx_extractor":
        return DocxExtractor.extract(path)
    if extractor == "html_extractor":
        return HtmlExtractor.extract(path)
    if extractor == "pptx_extractor":
        from .pptx_extractor import PptxExtractor  # noqa: PLC0415
        return PptxExtractor.extract(path)
    if extractor == "parquet_adapter":
        from .parquet_adapter import ParquetAdapter  # noqa: PLC0415
        return ParquetAdapter.extract(path)
    if extractor == "image_adapter":
        from .image_adapter import ImageAdapter  # noqa: PLC0415
        return ImageAdapter.extract(path)

    raise ValueError(f"No extractor found for: {path}")
