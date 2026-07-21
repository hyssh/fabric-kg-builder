"""Standalone PNG / JPEG / TIFF image source adapter (EXT-005).

Reads image metadata via Pillow and produces:
- One ``SourceFileRow``.
- One ``DocumentElementRow(element_type="image_ref")`` with EXIF/dimension metadata.
- Zero or more ``DocumentElementRow(element_type="ocr_text")`` rows when an
  injected OCR provider is supplied (never invented).
- Zero or one ``DocumentElementRow(element_type="vision_description")`` when an
  injected vision provider is supplied (never invented).

OCR / vision results
--------------------
Pass callables via *ocr_provider* and *vision_provider*:

    result = ImageAdapter.extract(
        path,
        ocr_provider=lambda p: my_di_layout_client.analyze(p),
        vision_provider=lambda p: my_gpt4v_client.describe(p),
    )

When a provider is absent the matching ``extra_meta`` status key is the
literal string ``"unavailable"`` — not ``None`` — so callers can distinguish
"not run" from "ran but found nothing".

Limits (EXT-009)
----------------
- File size: ``adapter.MAX_FILE_BYTES`` (200 MB).
- Pixel count: ``adapter.MAX_IMAGE_PIXELS`` (300 MP) — enforced before decompression.
- Supported formats: PNG, JPEG, TIFF.
- PSD is explicitly unsupported (truthful typed status).
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fabric_kg_builder.model.ids import (
    content_hash as compute_content_hash,
    make_document_element_id,
    make_source_file_id,
)
from fabric_kg_builder.model.schemas import DocumentElementRow, SourceFileRow

from .adapter import (
    ADAPTER_CONTRACT_VERSION,
    MAX_FILE_BYTES,
    MAX_IMAGE_PIXELS,
    AdapterError,
    AdapterResult,
    FailureType,
    check_file_size,
)
from .media_type import validate_extension_vs_signature

_ADAPTER_NAME = "image_adapter"
_ADAPTER_VERSION = "1.0.0"

_SUPPORTED_IMAGE_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".tiff", ".tif"}
)
_UNSUPPORTED_IMAGE_EXTS: frozenset[str] = frozenset({".psd"})

_LINEAGE_KEYS: frozenset[str] = frozenset(
    {"project_id", "asset_id", "asset_version_id", "run_id", "domain_hash"}
)


# ---------------------------------------------------------------------------
# Provider result types (EXT-005)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OcrRegion:
    """Single OCR text region returned by an injected DI/Layout provider."""

    text: str
    page_number: int = 1
    confidence: float | None = None
    # [x0, y0, x1, y1] in pixel or normalised coordinates, as supplied by provider
    bounding_box: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class VisionDescription:
    """Vision model output from an injected vision provider (never invented)."""

    description: str
    model: str | None = None
    confidence: float | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _file_hash(path: Path) -> str:
    """SHA-256 via streaming read to avoid loading the whole file into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_path(path: Path, project_root: Path | None) -> str:
    root = project_root or Path.cwd()
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _safe_exif(image: Any) -> dict[str, Any]:
    """Extract EXIF metadata from a Pillow image; returns empty dict on any failure."""
    try:
        from PIL.ExifTags import TAGS  # noqa: PLC0415

        exif_data = image.getexif()
        if not exif_data:
            return {}
        result: dict[str, Any] = {}
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            # Only keep JSON-serialisable scalars; decode bytes to string
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if isinstance(value, (str, int, float)):
                result[str(tag)] = value
        return result
    except (AttributeError, TypeError, struct.error):
        return {}


class ImageAdapter:
    """Source adapter for PNG / JPEG / TIFF image files (EXT-005)."""

    @staticmethod
    def extract(
        path: str | Path,
        *,
        project_root: str | Path | None = None,
        validate_mime: bool = True,
        max_bytes: int = MAX_FILE_BYTES,
        max_pixels: int = MAX_IMAGE_PIXELS,
        lineage: dict | None = None,
        ocr_provider: Callable[[Path], list[OcrRegion]] | None = None,
        vision_provider: Callable[[Path], VisionDescription] | None = None,
        blob_locator: str | None = None,
    ) -> AdapterResult:
        """Read image metadata and optionally OCR/vision outputs from *path*.

        Parameters
        ----------
        path:
            Path to a PNG, JPEG, or TIFF file.
        project_root:
            Optional root for canonical relative-path computation.
        validate_mime:
            When True (default), validate magic bytes match the declared extension.
        max_bytes:
            Reject files exceeding this byte size (EXT-009).
        max_pixels:
            Reject images exceeding this pixel count (EXT-009, prevents decompression
            bomb). Checked via lazy PIL open before full decompression.
        lineage:
            Optional dict with lineage envelope keys forwarded to all emitted rows.
        ocr_provider:
            Optional callable ``(Path) -> list[OcrRegion]``.  When supplied the
            adapter calls it and emits one ``element_type="ocr_text"``
            ``DocumentElementRow`` per region.  When absent,
            ``extra_meta["ocr_status"]`` is set to the literal string
            ``"unavailable"`` — never ``None``.
        vision_provider:
            Optional callable ``(Path) -> VisionDescription``.  When supplied the
            adapter calls it and emits one ``element_type="vision_description"``
            ``DocumentElementRow``.  When absent,
            ``extra_meta["vision_status"]`` is ``"unavailable"``.
        blob_locator:
            Optional pre-computed Blob Storage URI for the uploaded image.  When
            provided it becomes ``AdapterResult.source_locator`` and is also
            stored in ``extra_meta["image_locators"]["blob"]``.  The original
            local path is always stored in ``extra_meta["image_locators"]["local"]``.

        Returns
        -------
        AdapterResult
            ``adapter_name="image_adapter"``.
            ``document_elements`` contains:

            * one ``image_ref`` element (always),
            * zero-or-more ``ocr_text`` elements (when *ocr_provider* given),
            * zero-or-one ``vision_description`` element (when *vision_provider* given).

            ``extra_meta`` keys:

            * ``width``, ``height``, ``mode``, ``format``, ``dpi_x``, ``dpi_y``, ``exif``
            * ``ocr_status``: ``"provided"`` or ``"unavailable"``
            * ``ocr_regions``: list of region dicts (populated when *ocr_provider* given)
            * ``vision_status``: ``"provided"`` or ``"unavailable"``
            * ``vision_description``: description string or ``None``
            * ``vision_model``: model identifier string or ``None``
            * ``image_locators``: ``{"local": "file://...", "blob": str|None}``

        Raises
        ------
        AdapterError(UNSUPPORTED)
            For ``.psd`` files or when PSD magic is detected regardless of extension.
        AdapterError(MIME_MISMATCH)
            When magic bytes contradict the declared extension.
        AdapterError(TOO_LARGE)
            When the file exceeds *max_bytes* or image pixel count exceeds *max_pixels*.
        AdapterError(CORRUPT)
            When Pillow cannot open the file.
        """
        lineage = lineage or {}
        lineage_kwargs = {k: v for k, v in lineage.items() if k in _LINEAGE_KEYS}
        path = Path(path)

        if path.suffix.lower() in _UNSUPPORTED_IMAGE_EXTS:
            raise AdapterError(
                FailureType.UNSUPPORTED,
                f"File type '{path.suffix.lower()}' (PSD) is explicitly "
                "unsupported pending feasibility evaluation.",
                source_locator=str(path),
            )

        if not path.exists():
            raise AdapterError(
                FailureType.NOT_FOUND,
                f"Source file not found: {path}",
                source_locator=str(path),
            )

        check_file_size(path, max_bytes)

        # Detect PSD regardless of extension before any MIME check
        try:
            with open(path, "rb") as fh:
                header_bytes = fh.read(4)
            if header_bytes == b"8BPS":
                raise AdapterError(
                    FailureType.UNSUPPORTED,
                    f"File '{path.name}' has PSD magic bytes (8BPS) and is "
                    "explicitly unsupported, regardless of its extension.",
                    source_locator=str(path),
                )
        except OSError:
            pass

        detected_mime = "image/png"  # default; overwritten below
        if validate_mime:
            detected_mime = validate_extension_vs_signature(path)

        now = datetime.now(timezone.utc)
        file_hash = _file_hash(path)
        can_path = _canonical_path(path, Path(project_root) if project_root else None)
        source_file_id = make_source_file_id(can_path, file_hash)

        # Read image metadata via Pillow; check pixel count before full decompression
        try:
            from PIL import Image  # noqa: PLC0415

            with Image.open(path) as img:
                width, height = img.size
                # Pixel limit check BEFORE img.load() to avoid decompression bomb
                if width * height > max_pixels:
                    raise AdapterError(
                        FailureType.TOO_LARGE,
                        f"Image '{path.name}' is {width}×{height} = "
                        f"{width * height:,} pixels, which exceeds the "
                        f"{max_pixels:,}-pixel limit.",
                        source_locator=str(path),
                    )
                # Verify content is valid by loading pixel data
                img.load()
                mode = img.mode
                img_format = img.format or path.suffix.lstrip(".").upper()
                dpi = img.info.get("dpi")
                if isinstance(dpi, (list, tuple)) and len(dpi) >= 2:
                    dpi_x, dpi_y = float(dpi[0]), float(dpi[1])
                else:
                    dpi_x = dpi_y = None
                exif = _safe_exif(img)
        except AdapterError:
            raise
        except ImportError as exc:
            raise ImportError(
                "Pillow is required to use the image adapter. "
                "Install it with: pip install pillow"
            ) from exc
        except Exception as exc:
            raise AdapterError(
                FailureType.CORRUPT,
                f"Cannot open image '{path.name}': {exc}",
                source_locator=str(path),
            ) from exc

        local_locator = f"file://{path.resolve().as_posix()}"
        canonical_locator = blob_locator if blob_locator is not None else local_locator

        source_file = SourceFileRow(
            source_file_id=source_file_id,
            path=can_path,
            filename=path.name,
            source_type=path.suffix.lstrip(".").lower(),
            content_hash=file_hash,
            byte_size=path.stat().st_size,
            ingested_at=now,
            notes=f"format={img_format}; mode={mode}; {width}x{height}",
            **lineage_kwargs,
        )

        content = f"{path.name} [{img_format} {width}x{height} {mode}]"
        content_hash_val = compute_content_hash(content)
        elem_id = make_document_element_id(
            source_file_id, "image_ref", None, 0, content_hash_val
        )

        document_elements: list[DocumentElementRow] = [
            DocumentElementRow(
                document_element_id=elem_id,
                source_file_id=source_file_id,
                element_type="image_ref",
                title=path.name,
                content=content,
                sort_order=0,
                content_hash=content_hash_val,
                extracted_at=now,
                **lineage_kwargs,
            )
        ]

        # ------------------------------------------------------------------
        # OCR provider (EXT-005) — never invented; explicit unavailable state
        # ------------------------------------------------------------------
        ocr_regions: list[OcrRegion] = []
        if ocr_provider is not None:
            ocr_regions = list(ocr_provider(path))
            for idx, region in enumerate(ocr_regions, start=1):
                region_hash = compute_content_hash(
                    f"{region.text}:{region.page_number}:{region.confidence}"
                )
                region_locator = json.dumps(
                    {
                        "bounding_box": region.bounding_box or [],
                        "confidence": region.confidence,
                        "page_number": region.page_number,
                    },
                    separators=(",", ":"),
                )
                document_elements.append(
                    DocumentElementRow(
                        document_element_id=make_document_element_id(
                            source_file_id, "ocr_text", region.page_number, idx, region_hash
                        ),
                        source_file_id=source_file_id,
                        element_type="ocr_text",
                        content=region.text,
                        page_number=region.page_number,
                        sort_order=idx,
                        content_hash=region_hash,
                        extracted_at=now,
                        parent_record_id=elem_id,
                        source_locator_json=region_locator,
                        **lineage_kwargs,
                    )
                )

        # ------------------------------------------------------------------
        # Vision provider (EXT-005) — never invented; explicit unavailable state
        # ------------------------------------------------------------------
        vision_desc: VisionDescription | None = None
        if vision_provider is not None:
            vision_desc = vision_provider(path)
            vdesc_hash = compute_content_hash(vision_desc.description)
            vdesc_locator = json.dumps(
                {"model": vision_desc.model, "confidence": vision_desc.confidence},
                separators=(",", ":"),
            )
            document_elements.append(
                DocumentElementRow(
                    document_element_id=make_document_element_id(
                        source_file_id,
                        "vision_description",
                        None,
                        len(document_elements),
                        vdesc_hash,
                    ),
                    source_file_id=source_file_id,
                    element_type="vision_description",
                    content=vision_desc.description,
                    sort_order=len(document_elements),
                    content_hash=vdesc_hash,
                    extracted_at=now,
                    parent_record_id=elem_id,
                    source_locator_json=vdesc_locator,
                    **lineage_kwargs,
                )
            )

        extra_meta: dict[str, Any] = {
            "width": width,
            "height": height,
            "mode": mode,
            "format": img_format,
            "dpi_x": dpi_x,
            "dpi_y": dpi_y,
            "exif": exif,
            # OCR: explicit status — never None when no provider
            "ocr_status": "provided" if ocr_provider is not None else "unavailable",
            "ocr_regions": [
                {
                    "text": r.text,
                    "page_number": r.page_number,
                    "confidence": r.confidence,
                    "bounding_box": r.bounding_box or [],
                }
                for r in ocr_regions
            ],
            # Vision: explicit status — never None when no provider
            "vision_status": "provided" if vision_provider is not None else "unavailable",
            "vision_description": vision_desc.description if vision_desc else None,
            "vision_model": vision_desc.model if vision_desc else None,
            # Blob / local image lineage
            "image_locators": {
                "local": local_locator,
                "blob": blob_locator,
            },
        }

        return AdapterResult(
            adapter_name=_ADAPTER_NAME,
            adapter_version=_ADAPTER_VERSION,
            detected_media_type=detected_mime,
            source_locator=canonical_locator,
            source_file=source_file,
            document_elements=document_elements,
            extra_meta=extra_meta,
        )
