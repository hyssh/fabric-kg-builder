"""Normalized SourceAdapter protocol and result contract (EXT-001).

All source adapters MUST produce an ``AdapterResult``; typed failures MUST
raise ``AdapterError`` — never silence or swallow exceptions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from fabric_kg_builder.model.schemas import DocumentElementRow, SourceFileRow

ADAPTER_CONTRACT_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Resource limits (EXT-009)
# ---------------------------------------------------------------------------

MAX_FILE_BYTES: int = 200 * 1024 * 1024   # 200 MB
MAX_PAGES: int = 2_000
MAX_ROWS: int = 5_000_000
MAX_SLIDES: int = 1_000
MAX_COMPRESSION_RATIO: float = 100.0   # per-member uncompressed / compressed cap
MAX_IMAGE_PIXELS: int = 300_000_000    # 300 MP decompression cap for raster images

# OOXML archive safety limits (EXT-009)
OOXML_MAX_MEMBERS: int = 10_000
OOXML_MAX_UNCOMPRESSED_BYTES: int = 500 * 1024 * 1024  # 500 MB


# ---------------------------------------------------------------------------
# Typed failure enumeration (EXT-009)
# ---------------------------------------------------------------------------


class FailureType(str, Enum):
    """Typed failure categories — never silenced by adapters."""

    ENCRYPTED = "encrypted"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too_large"
    TOO_MANY_ROWS = "too_many_rows"
    TOO_MANY_PAGES = "too_many_pages"
    TOO_MANY_SLIDES = "too_many_slides"
    COMPRESSION_RATIO = "compression_ratio"
    MIME_MISMATCH = "mime_mismatch"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class AdapterError(ValueError):
    """Raised by source adapters for typed, non-silent failures."""

    def __init__(
        self,
        failure_type: FailureType,
        message: str,
        source_locator: str = "",
    ) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.source_locator = source_locator

    def to_dict(self) -> dict[str, str]:
        return {
            "failure_type": self.failure_type.value,
            "message": str(self),
            "source_locator": self.source_locator,
        }


# ---------------------------------------------------------------------------
# Hyperlink record (EXT-003)
# ---------------------------------------------------------------------------


@dataclass
class HyperlinkRecord:
    """A hyperlink preserved from a source document with positional context."""

    anchor: str            # display / anchor text
    target: str            # URL or internal target reference
    source_element_id: str | None = None   # ID of the containing document element
    page_number: int | None = None
    sort_order: int | None = None
    source_locator_json: str | None = None  # JSON with structured positional context


# ---------------------------------------------------------------------------
# Normalized adapter result envelope (EXT-001)
# ---------------------------------------------------------------------------


@dataclass
class AdapterResult:
    """Normalized result envelope for all source adapters (EXT-001).

    Mandatory fields
    ----------------
    adapter_name, adapter_version, detected_media_type, source_locator,
    source_file, document_elements.

    Optional fields
    ---------------
    page_count        for paginated sources (PDF, PPTX, DOCX).
    hyperlinks        preserved hyperlinks with anchor/target/position (EXT-003).
    extra_meta        adapter-specific metadata dict (row_groups, schema, etc.).
    """

    adapter_name: str
    adapter_version: str
    detected_media_type: str
    source_locator: str
    source_file: SourceFileRow
    document_elements: list[DocumentElementRow]
    page_count: int | None = None
    hyperlinks: list[HyperlinkRecord] = field(default_factory=list)
    extra_meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# File-size guard (EXT-009)
# ---------------------------------------------------------------------------


def check_file_size(path: Path, max_bytes: int = MAX_FILE_BYTES) -> None:
    """Raise ``AdapterError(TOO_LARGE)`` when *path* exceeds *max_bytes*."""
    size = path.stat().st_size
    if size > max_bytes:
        raise AdapterError(
            FailureType.TOO_LARGE,
            f"File '{path.name}' is {size:,} bytes, which exceeds the "
            f"{max_bytes:,}-byte limit.",
            source_locator=str(path),
        )
