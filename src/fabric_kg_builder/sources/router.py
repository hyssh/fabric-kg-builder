"""Extractor routing by source file extension."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .csv_loader import load_csv
from .docx_extractor import DocxExtractor
from .html_extractor import HtmlExtractor
from .pdf_extractor import PdfExtractor


def route(path: str | Path) -> str:
    """Return the extractor name for the given file path."""
    suffix = Path(path).suffix.lower()
    if suffix in {".csv", ".tsv", ".xlsx"}:
        return "csv_loader"
    if suffix == ".pdf":
        return "pdf_extractor"
    if suffix == ".docx":
        return "docx_extractor"
    if suffix in {".html", ".htm", ".md"}:
        return "html_extractor"
    raise ValueError(f"Unsupported source extension: {suffix or '<none>'}")


def extract(path: str | Path) -> Any:
    """Extract document elements from a file, dispatching by extension."""
    extractor = route(path)
    if extractor == "csv_loader":
        return load_csv(path)
    if extractor == "pdf_extractor":
        return PdfExtractor.extract(path)
    if extractor == "docx_extractor":
        return DocxExtractor.extract(path)
    return HtmlExtractor.extract(path)
