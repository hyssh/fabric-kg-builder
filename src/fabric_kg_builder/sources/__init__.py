"""Source file loading and extraction."""

from .adapter import (
    ADAPTER_CONTRACT_VERSION,
    MAX_FILE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_PAGES,
    MAX_ROWS,
    MAX_SLIDES,
    OOXML_MAX_MEMBERS,
    OOXML_MAX_UNCOMPRESSED_BYTES,
    AdapterError,
    AdapterResult,
    FailureType,
    HyperlinkRecord,
    check_file_size,
)
from .checkpoint import (
    CheckpointRecord,
    CheckpointStore,
    compute_checkpoint_fingerprint,
)
from .chunker import ChunkResult, Chunker
from .csv_loader import CsvLoadResult, CsvLoaderError, load_csv
from .docintel_normalizer import (
    DiNormalizedLayout,
    normalize_di_layout,
)
from .docx_extractor import DocxExtractResult, DocxExtractor
from .html_extractor import HtmlExtractResult, HtmlExtractor
from .image_adapter import ImageAdapter, OcrRegion, VisionDescription
from .media_type import (
    detect_media_type,
    mime_for_extension,
    validate_extension_vs_signature,
    validate_ooxml_archive,
)
from .parquet_adapter import ParquetAdapter
from .pdf_extractor import PdfExtractResult, PdfExtractor
from .pptx_extractor import PptxExtractor
from .router import extract, route
from .table_extractor import TableExtractor

__all__ = [
    # Adapter contract (EXT-001)
    "ADAPTER_CONTRACT_VERSION",
    "MAX_FILE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_PAGES",
    "MAX_ROWS",
    "MAX_SLIDES",
    "OOXML_MAX_MEMBERS",
    "OOXML_MAX_UNCOMPRESSED_BYTES",
    "AdapterError",
    "AdapterResult",
    "FailureType",
    "HyperlinkRecord",
    "check_file_size",
    # Checkpoint (EXT-008)
    "CheckpointRecord",
    "CheckpointStore",
    "compute_checkpoint_fingerprint",
    # Media type (EXT-002)
    "detect_media_type",
    "mime_for_extension",
    "validate_extension_vs_signature",
    "validate_ooxml_archive",
    # DI normalizer (EXT-007)
    "DiNormalizedLayout",
    "normalize_di_layout",
    # Existing extractors
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
    # New adapters
    "ParquetAdapter",
    "ImageAdapter",
    "OcrRegion",
    "VisionDescription",
    "PptxExtractor",
    # Router
    "route",
    "extract",
]
