"""Media-type detection via file magic bytes and MIME-routing validation (EXT-002).

Rejects mismatched or unsafe declared extensions instead of silently selecting
the wrong parser.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from .adapter import (
    MAX_COMPRESSION_RATIO,
    OOXML_MAX_MEMBERS,
    OOXML_MAX_UNCOMPRESSED_BYTES,
    AdapterError,
    FailureType,
)

# ---------------------------------------------------------------------------
# Signature table: (magic_bytes, offset, mime_type)
# ---------------------------------------------------------------------------

_SIGNATURES: list[tuple[bytes, int, str]] = [
    (b"%PDF", 0, "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"II*\x00", 0, "image/tiff"),          # little-endian TIFF
    (b"MM\x00*", 0, "image/tiff"),          # big-endian TIFF
    (b"PAR1", 0, "application/x-parquet"),  # Parquet header magic
    (b"PARE", 0, "application/x-parquet"),  # Parquet alternative
    (b"PK\x03\x04", 0, "application/zip"),  # ZIP / Office Open XML
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", 0, "application/vnd.ms-excel"),
    (b"8BPS", 0, "image/vnd.adobe.photoshop"),  # PSD — actual 4-byte signature
]

# Extension → declared MIME
_EXT_TO_MIME: dict[str, str] = {
    ".pdf":     "application/pdf",
    ".docx":    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls":     "application/vnd.ms-excel",
    ".xlsx":    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx":    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv":     "text/csv",
    ".tsv":     "text/tab-separated-values",
    ".html":    "text/html",
    ".htm":     "text/html",
    ".md":      "text/markdown",
    ".png":     "image/png",
    ".jpg":     "image/jpeg",
    ".jpeg":    "image/jpeg",
    ".tiff":    "image/tiff",
    ".tif":     "image/tiff",
    ".parquet": "application/x-parquet",
    ".psd":     "image/vnd.adobe.photoshop",
}

# ZIP-based Office Open XML subtypes
_ZIP_SUBTYPES: frozenset[str] = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)

# Explicitly unsupported MIME types (truthful typed status — EXT-009)
_UNSUPPORTED_MIME: frozenset[str] = frozenset({"image/vnd.adobe.photoshop"})


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def detect_media_type(path: Path) -> str:
    """Detect media type from the first 32 bytes of *path* (bounded read).

    Returns a MIME string.  Unknown files return ``"application/octet-stream"``.
    For ZIP-based files, the content-types entry disambiguates DOCX/XLSX/PPTX.
    """
    try:
        with open(path, "rb") as fh:
            header = fh.read(32)
    except OSError:
        return "application/octet-stream"

    for sig, offset, mime in _SIGNATURES:
        if header[offset : offset + len(sig)] == sig:
            if mime == "application/zip":
                return _resolve_zip_subtype(path)
            return mime

    # Heuristic: if header bytes are all printable ASCII / UTF-8 → text/plain
    try:
        header.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        pass

    return "application/octet-stream"


def _resolve_zip_subtype(path: Path) -> str:
    """Distinguish DOCX / XLSX / PPTX by reading ``[Content_Types].xml``."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            try:
                ct = zf.read("[Content_Types].xml").decode("utf-8", errors="ignore")
            except KeyError:
                return "application/zip"
    except (zipfile.BadZipFile, OSError):
        return "application/zip"

    if "wordprocessingml" in ct:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if "spreadsheetml" in ct:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if "presentationml" in ct:
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return "application/zip"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_extension_vs_signature(path: Path) -> str:
    """Detect the actual media type and reject extension/signature mismatches.

    Parameters
    ----------
    path:
        Existing file to inspect.

    Returns
    -------
    str
        The *detected* MIME type on success (or the declared type when detection
        is ambiguous for text files).

    Raises
    ------
    AdapterError(UNSUPPORTED)
        When the declared or detected type is explicitly unsupported (e.g. PSD).
    AdapterError(MIME_MISMATCH)
        When the file's magic bytes contradict the declared extension.
    """
    ext = path.suffix.lower()
    declared_mime = _EXT_TO_MIME.get(ext)
    detected_mime = detect_media_type(path)

    # Unsupported check first (both declared and detected)
    if declared_mime in _UNSUPPORTED_MIME:
        raise AdapterError(
            FailureType.UNSUPPORTED,
            f"File type '{ext}' ({declared_mime}) is explicitly unsupported. "
            "(PSD support is pending feasibility evaluation.)",
            source_locator=str(path),
        )
    if detected_mime in _UNSUPPORTED_MIME:
        raise AdapterError(
            FailureType.UNSUPPORTED,
            f"Detected media type '{detected_mime}' is explicitly unsupported. "
            "(PSD support is pending feasibility evaluation.)",
            source_locator=str(path),
        )

    if declared_mime is None:
        # Unknown extension — return what we detected without rejecting
        return detected_mime

    # ZIP-based Office Open XML: detected may be the specific subtype or
    # the generic "application/zip" — either is acceptable.
    if declared_mime in _ZIP_SUBTYPES:
        if (
            declared_mime
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            and detected_mime == "application/vnd.ms-excel"
        ):
            # Legacy Excel is common in enterprise archives even when the file
            # was renamed with an .xlsx suffix. Route it to the XLS adapter.
            return detected_mime
        if detected_mime not in (declared_mime, "application/zip"):
            raise AdapterError(
                FailureType.MIME_MISMATCH,
                f"Extension '{ext}' declares {declared_mime!r} but file "
                f"signature suggests {detected_mime!r}.",
                source_locator=str(path),
            )
        return declared_mime

    # Text MIME types: signature detection can only distinguish
    # "text/plain" or "application/octet-stream" for text files.
    # Accept if declared is text/* and detected is text/plain or matches exactly.
    if declared_mime.startswith("text/"):
        if detected_mime in ("text/plain", declared_mime):
            return declared_mime
        if detected_mime == "application/octet-stream":
            # May be a UTF-16/binary CSV; trust the extension
            return declared_mime
        raise AdapterError(
            FailureType.MIME_MISMATCH,
            f"Extension '{ext}' declares {declared_mime!r} but file "
            f"signature suggests {detected_mime!r}.",
            source_locator=str(path),
        )

    # Exact match or application/octet-stream fallback for parquet
    if ext == ".parquet" and detected_mime == "application/octet-stream":
        # Parquet footer check from end of file
        if _check_parquet_magic(path):
            return declared_mime
        raise AdapterError(
            FailureType.MIME_MISMATCH,
            f"File claimed to be Parquet but lacks PAR1 footer magic.",
            source_locator=str(path),
        )

    if detected_mime != declared_mime:
        raise AdapterError(
            FailureType.MIME_MISMATCH,
            f"Extension '{ext}' declares {declared_mime!r} but file "
            f"signature suggests {detected_mime!r}.",
            source_locator=str(path),
        )

    return detected_mime


def _check_parquet_magic(path: Path) -> bool:
    """Verify PAR1 magic at file start or end (bounded reads only)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
            fh.seek(-4, 2)  # 4 bytes from EOF
            tail = fh.read(4)
        return head == b"PAR1" or tail == b"PAR1"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# OOXML archive safety validator (EXT-009)
# ---------------------------------------------------------------------------


def validate_ooxml_archive(path: Path, declared_mime: str) -> None:
    """Validate a ZIP-based OOXML archive for safety before handing to a parser.

    Checks performed *without* decompressing any member content:
    - Member count ≤ ``OOXML_MAX_MEMBERS``
    - Total uncompressed size ≤ ``OOXML_MAX_UNCOMPRESSED_BYTES``
    - Per-member compression ratio ≤ ``MAX_COMPRESSION_RATIO``
    - No encrypted entries (central-directory flag bit 0x0001)
    - ``[Content_Types].xml`` present and subtype matches *declared_mime*

    Raises
    ------
    AdapterError(CORRUPT)
        When the file is not a valid ZIP or lacks ``[Content_Types].xml``.
    AdapterError(ENCRYPTED)
        When any member has the encrypted flag set.
    AdapterError(COMPRESSION_RATIO)
        When any member's ratio exceeds the safety cap.
    AdapterError(TOO_LARGE)
        When member count or total uncompressed size exceeds the limit.
    AdapterError(MIME_MISMATCH)
        When ``[Content_Types].xml`` does not match the declared OOXML subtype,
        indicating a generic ZIP renamed as an Office document.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            infos = zf.infolist()

            if len(infos) > OOXML_MAX_MEMBERS:
                raise AdapterError(
                    FailureType.TOO_LARGE,
                    f"OOXML archive '{path.name}' has {len(infos):,} members "
                    f"(limit: {OOXML_MAX_MEMBERS:,}).",
                    str(path),
                )

            total_uncompressed = 0
            for info in infos:
                # Bit 0 of flag_bits = general-purpose bit flag encryption
                if info.flag_bits & 0x1:
                    raise AdapterError(
                        FailureType.ENCRYPTED,
                        f"OOXML archive '{path.name}' contains an encrypted "
                        f"entry: '{info.filename}'.",
                        str(path),
                    )
                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > MAX_COMPRESSION_RATIO:
                        raise AdapterError(
                            FailureType.COMPRESSION_RATIO,
                            f"Member '{info.filename}' in '{path.name}' has a "
                            f"{ratio:.1f}x compression ratio (limit: "
                            f"{MAX_COMPRESSION_RATIO}x). Possible archive bomb.",
                            str(path),
                        )
                total_uncompressed += info.file_size

            if total_uncompressed > OOXML_MAX_UNCOMPRESSED_BYTES:
                raise AdapterError(
                    FailureType.TOO_LARGE,
                    f"OOXML archive '{path.name}' would expand to "
                    f"{total_uncompressed:,} bytes (limit: "
                    f"{OOXML_MAX_UNCOMPRESSED_BYTES:,} bytes).",
                    str(path),
                )

            # Validate [Content_Types].xml matches declared OOXML subtype
            try:
                ct_bytes = zf.read("[Content_Types].xml")
            except KeyError:
                raise AdapterError(
                    FailureType.CORRUPT,
                    f"'{path.name}' is missing '[Content_Types].xml' — "
                    "not a valid OOXML file (generic ZIP renamed as Office?)",
                    str(path),
                )
            ct = ct_bytes.decode("utf-8", errors="ignore")

    except AdapterError:
        raise
    except zipfile.BadZipFile as exc:
        raise AdapterError(
            FailureType.CORRUPT,
            f"'{path.name}' is not a valid ZIP/OOXML archive: {exc}",
            str(path),
        ) from exc
    except OSError as exc:
        raise AdapterError(
            FailureType.NOT_FOUND,
            f"Cannot read archive '{path.name}': {exc}",
            str(path),
        ) from exc

    # Subtype content-type check — reject generic ZIPs masquerading as Office
    if "wordprocessingml" in declared_mime and "wordprocessingml" not in ct:
        raise AdapterError(
            FailureType.MIME_MISMATCH,
            f"'{path.name}' is declared as DOCX (wordprocessingml) but "
            f"[Content_Types].xml does not confirm that subtype.",
            str(path),
        )
    if "spreadsheetml" in declared_mime and "spreadsheetml" not in ct:
        raise AdapterError(
            FailureType.MIME_MISMATCH,
            f"'{path.name}' is declared as XLSX (spreadsheetml) but "
            f"[Content_Types].xml does not confirm that subtype.",
            str(path),
        )
    if "presentationml" in declared_mime and "presentationml" not in ct:
        raise AdapterError(
            FailureType.MIME_MISMATCH,
            f"'{path.name}' is declared as PPTX (presentationml) but "
            f"[Content_Types].xml does not confirm that subtype.",
            str(path),
        )


def mime_for_extension(ext: str) -> str | None:
    """Return the declared MIME type for a file extension, or None if unknown."""
    return _EXT_TO_MIME.get(ext.lower())
