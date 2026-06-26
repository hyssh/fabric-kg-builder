"""Source file loading and extraction."""

from .chunker import ChunkResult, Chunker
from .csv_loader import CsvLoadResult, CsvLoaderError, load_csv
from .docx_extractor import DocxExtractResult, DocxExtractor
from .html_extractor import HtmlExtractResult, HtmlExtractor
from .pdf_extractor import PdfExtractResult, PdfExtractor
from .router import extract, route
from .table_extractor import TableExtractor

__all__ = [
    "load_csv",
    "CsvLoadResult",
    "CsvLoaderError",
    "PdfExtractor",
    "PdfExtractResult",
    "DocxExtractor",
    "DocxExtractResult",
    "HtmlExtractor",
    "HtmlExtractResult",
    "Chunker",
    "ChunkResult",
    "TableExtractor",
    "route",
    "extract",
]
