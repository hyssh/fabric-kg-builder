"""Technical-drawing tiling, metadata normalization, and topology candidates."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, Field, model_validator

from fabric_kg_builder.model.ids import (
    content_hash as compute_content_hash,
    make_document_element_id,
    make_id,
)
from fabric_kg_builder.model.schemas import DocumentElementRow
from fabric_kg_builder.lineage.common import safe_original_name


class DrawingLimitError(ValueError):
    """Raised when a drawing exceeds configured processing limits."""


class DrawingConfig(BaseModel):
    tile_size: int = Field(default=1_024, ge=256, le=4_096)
    tile_overlap_ratio: float = Field(default=0.05, ge=0.0, le=0.25)
    pyramid_scales: tuple[float, ...] = (1.0, 0.5, 0.25)
    preview_max_dimension: int = Field(default=2_048, ge=256)
    render_dpi: int = Field(default=200, ge=72, le=600)
    max_pixels_per_sheet: int = Field(default=150_000_000, ge=1_000_000)
    review_confidence_threshold: float = Field(default=0.65, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_scales(self) -> "DrawingConfig":
        if not self.pyramid_scales:
            raise ValueError("pyramid_scales must contain at least one scale")
        if any(scale <= 0 or scale > 1 for scale in self.pyramid_scales):
            raise ValueError("pyramid scales must be greater than 0 and at most 1")
        if len(set(self.pyramid_scales)) != len(self.pyramid_scales):
            raise ValueError("pyramid scales must be unique")
        return self


class CoordinateTransform(BaseModel):
    """Transform between one tile and original-sheet pixel coordinates."""

    scale: float = Field(gt=0)
    origin_x: float = Field(ge=0)
    origin_y: float = Field(ge=0)
    tile_width: int = Field(gt=0)
    tile_height: int = Field(gt=0)
    original_width: int = Field(gt=0)
    original_height: int = Field(gt=0)

    def tile_to_original(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.origin_x + (x / self.scale),
            self.origin_y + (y / self.scale),
        )

    def original_to_tile(self, x: float, y: float) -> tuple[float, float]:
        return (
            (x - self.origin_x) * self.scale,
            (y - self.origin_y) * self.scale,
        )


@dataclass(frozen=True)
class DrawingTile:
    tile_id: str
    sheet_number: int
    level: int
    kind: Literal["full_sheet", "tile"]
    image_bytes: bytes
    image_hash: str
    mime_type: str
    transform: CoordinateTransform


def drawing_tile_filename(tile_id: str, ext: str = "png") -> str:
    """Return a deterministic cross-platform filename for a canonical tile ID."""
    if not tile_id:
        raise ValueError("tile_id must not be empty")
    normalized_ext = ext.lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]+", normalized_ext):
        raise ValueError(f"Invalid drawing tile extension: {ext!r}")
    return safe_original_name(f"{tile_id}.{normalized_ext}")


class DrawingTextRegion(BaseModel):
    region_id: str
    text: str
    sheet_number: int = Field(default=1, ge=1)
    polygon: list[tuple[float, float]]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str = "document_intelligence_layout"
    source_locator_json: str | None = None

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        if not self.polygon:
            return (0.0, 0.0, 0.0, 0.0)
        xs = [point[0] for point in self.polygon]
        ys = [point[1] for point in self.polygon]
        return (min(xs), min(ys), max(xs), max(ys))


class DrawingSheetMetadata(BaseModel):
    sheet_number: int = Field(ge=1)
    drawing_id: str | None = None
    title: str | None = None
    revision: str | None = None
    scale_text: str | None = None
    units_text: str | None = None
    discipline: str | None = None
    legends: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    zones: list[str] = Field(default_factory=list)
    referenced_sheets: list[str] = Field(default_factory=list)
    evidence_region_ids: dict[str, list[str]] = Field(default_factory=dict)


class DetectorObservation(BaseModel):
    label: str
    observation_type: Literal["symbol", "callout", "dimension", "annotation"]
    bbox: tuple[float, float, float, float]
    sheet_number: int = Field(default=1, ge=1)
    method: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_region_ids: list[str] = Field(default_factory=list)


class ConnectorDetection(BaseModel):
    points: list[tuple[float, float]]
    sheet_number: int = Field(default=1, ge=1)
    arrow_at_start: bool = False
    arrow_at_end: bool = False
    method: str = "line_detector"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_points(self) -> "ConnectorDetection":
        if len(self.points) < 2:
            raise ValueError("connector requires at least two points")
        return self


class DrawingObservation(BaseModel):
    observation_id: str
    observation_type: Literal[
        "symbol",
        "callout",
        "dimension",
        "annotation",
        "connector",
    ]
    label: str | None = None
    sheet_number: int = Field(ge=1)
    geometry_json: str
    method: str
    confidence: float = Field(ge=0.0, le=1.0)
    review_state: Literal["not_required", "needs_review", "reviewed"]
    provenance_origin: Literal["observed", "inferred"]
    evidence_region_ids: list[str] = Field(default_factory=list)

    @property
    def geometry(self) -> dict:
        return json.loads(self.geometry_json)


class DrawingTopologyCandidate(BaseModel):
    topology_id: str
    relationship_type: Literal["connects_to", "flows_to"]
    source_observation_id: str | None = None
    target_observation_id: str | None = None
    connector_observation_id: str
    sheet_number: int = Field(ge=1)
    geometry_json: str
    method: str
    confidence: float = Field(ge=0.0, le=1.0)
    review_state: Literal["not_required", "needs_review", "reviewed"]
    provenance_origin: Literal["observed", "inferred"] = "inferred"
    dangling: bool = False


def _axis_starts(length: int, tile_size: int, overlap_pixels: int) -> list[int]:
    if length <= tile_size:
        return [0]
    stride = tile_size - overlap_pixels
    starts = list(range(0, max(length - tile_size + 1, 1), stride))
    final_start = length - tile_size
    if starts[-1] != final_start:
        starts.append(final_start)
    return starts


def _png_bytes(image: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _tile_pil_image(
    image: object,
    *,
    sheet_number: int,
    source_hash: str,
    config: DrawingConfig,
) -> list[DrawingTile]:
    from PIL import Image

    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL Image")
    original = image.convert("RGB")
    original_width, original_height = original.size
    if original_width * original_height > config.max_pixels_per_sheet:
        raise DrawingLimitError(
            f"Drawing sheet has {original_width * original_height:,} pixels; "
            f"limit is {config.max_pixels_per_sheet:,}."
        )

    tiles: list[DrawingTile] = []
    preview_scale = min(
        1.0,
        config.preview_max_dimension / max(original_width, original_height),
    )
    preview = original
    if preview_scale < 1.0:
        preview = original.resize(
            (
                max(1, round(original_width * preview_scale)),
                max(1, round(original_height * preview_scale)),
            ),
            Image.Resampling.LANCZOS,
        )
    preview_bytes = _png_bytes(preview)
    preview_hash = hashlib.sha256(preview_bytes).hexdigest()
    tiles.append(
        DrawingTile(
            tile_id=make_id(
                "dtile",
                f"{source_hash}:{sheet_number}:full:{preview_scale}",
            ),
            sheet_number=sheet_number,
            level=-1,
            kind="full_sheet",
            image_bytes=preview_bytes,
            image_hash=preview_hash,
            mime_type="image/png",
            transform=CoordinateTransform(
                scale=preview_scale,
                origin_x=0,
                origin_y=0,
                tile_width=preview.width,
                tile_height=preview.height,
                original_width=original_width,
                original_height=original_height,
            ),
        )
    )

    overlap_pixels = round(config.tile_size * config.tile_overlap_ratio)
    for level, scale in enumerate(config.pyramid_scales):
        level_image = original
        if scale != 1.0:
            level_image = original.resize(
                (
                    max(1, round(original_width * scale)),
                    max(1, round(original_height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
        width, height = level_image.size
        x_starts = _axis_starts(width, config.tile_size, overlap_pixels)
        y_starts = _axis_starts(height, config.tile_size, overlap_pixels)
        for y in y_starts:
            for x in x_starts:
                crop = level_image.crop(
                    (
                        x,
                        y,
                        min(x + config.tile_size, width),
                        min(y + config.tile_size, height),
                    )
                )
                data = _png_bytes(crop)
                tile_hash = hashlib.sha256(data).hexdigest()
                transform = CoordinateTransform(
                    scale=scale,
                    origin_x=x / scale,
                    origin_y=y / scale,
                    tile_width=crop.width,
                    tile_height=crop.height,
                    original_width=original_width,
                    original_height=original_height,
                )
                tiles.append(
                    DrawingTile(
                        tile_id=make_id(
                            "dtile",
                            f"{source_hash}:{sheet_number}:{level}:{x}:{y}",
                        ),
                        sheet_number=sheet_number,
                        level=level,
                        kind="tile",
                        image_bytes=data,
                        image_hash=tile_hash,
                        mime_type="image/png",
                        transform=transform,
                    )
                )
    return tiles


def tile_drawing(
    path: str | Path,
    *,
    config: DrawingConfig | None = None,
) -> list[DrawingTile]:
    """Create a full-sheet preview and multi-resolution tiles for PDF/raster input."""
    config = config or DrawingConfig()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Drawing source not found: {path}")
    source_hasher = hashlib.sha256()
    with path.open("rb") as source_stream:
        for chunk in iter(lambda: source_stream.read(64 * 1024), b""):
            source_hasher.update(chunk)
    source_hash = source_hasher.hexdigest()

    if path.suffix.lower() == ".pdf":
        import fitz
        from PIL import Image

        tiles: list[DrawingTile] = []
        scale = config.render_dpi / 72
        with fitz.open(path) as document:
            for page_index, page in enumerate(document, start=1):
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(scale, scale),
                    alpha=False,
                )
                mode = "RGB" if pixmap.n < 4 else "RGBA"
                image = Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)
                tiles.extend(
                    _tile_pil_image(
                        image,
                        sheet_number=page_index,
                        source_hash=source_hash,
                        config=config,
                    )
                )
        return tiles

    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        raise ValueError(
            "Technical drawing tiling supports PDF, PNG, JPEG, and TIFF inputs."
        )
    from PIL import Image

    with Image.open(path) as image:
        return _tile_pil_image(
            image,
            sheet_number=1,
            source_hash=source_hash,
            config=config,
        )


_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "drawing_id": re.compile(
        r"\b(?:drawing|dwg)\s*(?:no\.?|number|id)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/-]+)",
        re.IGNORECASE,
    ),
    "title": re.compile(r"\btitle\s*[:#-]\s*(.+)$", re.IGNORECASE),
    "revision": re.compile(r"\b(?:revision|rev\.?)\s*[:#-]?\s*([A-Z0-9.-]+)", re.IGNORECASE),
    "scale_text": re.compile(r"\bscale\s*[:#-]?\s*([A-Z0-9.:/-]+)", re.IGNORECASE),
    "units_text": re.compile(r"\bunits?\s*[:#-]?\s*([A-Z]+)", re.IGNORECASE),
    "discipline": re.compile(r"\bdiscipline\s*[:#-]?\s*(.+)$", re.IGNORECASE),
}
_SHEET_REFERENCE_RE = re.compile(
    r"\b(?:see|refer(?:ence)?(?:\s+to)?)\s+sheet\s+([A-Z0-9._/-]+)",
    re.IGNORECASE,
)
_ZONE_RE = re.compile(r"\bzone\s+([A-Z0-9-]+)", re.IGNORECASE)
_CALLOUT_RE = re.compile(r"^(?:[A-Z]{1,5}[- ]?)?\d{1,5}[A-Z]?$")
_DIMENSION_RE = re.compile(
    r"^(?:[ØR]\s*)?\d+(?:\.\d+)?\s*(?:mm|cm|m|in|ft|°|deg|±\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)


def extract_sheet_metadata(
    regions: Iterable[DrawingTextRegion],
    *,
    sheet_number: int,
    sheet_width: float,
    sheet_height: float,
) -> DrawingSheetMetadata:
    """Extract title-block and sheet metadata without inventing missing values."""
    metadata = DrawingSheetMetadata(sheet_number=sheet_number)
    matching_regions = [
        region for region in regions if region.sheet_number == sheet_number
    ]
    title_block_regions = [
        region
        for region in matching_regions
        if region.bbox[0] >= sheet_width * 0.45
        and region.bbox[1] >= sheet_height * 0.60
    ]
    parse_regions = title_block_regions or matching_regions

    for region in parse_regions:
        text = " ".join(region.text.split())
        for field_name, pattern in _FIELD_PATTERNS.items():
            if getattr(metadata, field_name) is not None:
                continue
            match = pattern.search(text)
            if match:
                setattr(metadata, field_name, match.group(1).strip())
                metadata.evidence_region_ids.setdefault(field_name, []).append(
                    region.region_id
                )

    for region in matching_regions:
        text = " ".join(region.text.split())
        lowered = text.lower()
        if "legend" in lowered:
            metadata.legends.append(text)
            metadata.evidence_region_ids.setdefault("legends", []).append(
                region.region_id
            )
        if lowered.startswith(("note", "general note")):
            metadata.notes.append(text)
            metadata.evidence_region_ids.setdefault("notes", []).append(
                region.region_id
            )
        for match in _SHEET_REFERENCE_RE.finditer(text):
            reference = match.group(1)
            if reference not in metadata.referenced_sheets:
                metadata.referenced_sheets.append(reference)
            metadata.evidence_region_ids.setdefault(
                "referenced_sheets", []
            ).append(region.region_id)
        for match in _ZONE_RE.finditer(text):
            zone = match.group(1)
            if zone not in metadata.zones:
                metadata.zones.append(zone)
            metadata.evidence_region_ids.setdefault("zones", []).append(
                region.region_id
            )
    return metadata


def _review_state(confidence: float, threshold: float) -> str:
    return "not_required" if confidence >= threshold else "needs_review"


def extract_drawing_observations(
    text_regions: Iterable[DrawingTextRegion],
    *,
    symbol_detections: Iterable[DetectorObservation] = (),
    connector_detections: Iterable[ConnectorDetection] = (),
    review_confidence_threshold: float = 0.65,
) -> list[DrawingObservation]:
    """Normalize OCR, symbol-detector, and connector outputs into observations."""
    observations: list[DrawingObservation] = []

    for region in text_regions:
        text = " ".join(region.text.split())
        if not text:
            continue
        observation_type: str | None = None
        if _DIMENSION_RE.fullmatch(text):
            observation_type = "dimension"
        elif _CALLOUT_RE.fullmatch(text):
            observation_type = "callout"
        elif text.lower().startswith(("note", "warning", "caution")):
            observation_type = "annotation"
        if observation_type is None:
            continue
        confidence = region.confidence if region.confidence is not None else 0.5
        geometry_json = json.dumps(
            {"bbox": region.bbox, "polygon": region.polygon},
            separators=(",", ":"),
        )
        observations.append(
            DrawingObservation(
                observation_id=make_id(
                    "dobs",
                    f"{region.sheet_number}:{observation_type}:{region.region_id}:{text}",
                ),
                observation_type=observation_type,
                label=text,
                sheet_number=region.sheet_number,
                geometry_json=geometry_json,
                method=region.method,
                confidence=confidence,
                review_state=_review_state(
                    confidence,
                    review_confidence_threshold,
                ),
                provenance_origin="observed",
                evidence_region_ids=[region.region_id],
            )
        )

    for detection in symbol_detections:
        geometry_json = json.dumps(
            {"bbox": detection.bbox},
            separators=(",", ":"),
        )
        observations.append(
            DrawingObservation(
                observation_id=make_id(
                    "dobs",
                    f"{detection.sheet_number}:{detection.observation_type}:"
                    f"{detection.label}:{detection.bbox}",
                ),
                observation_type=detection.observation_type,
                label=detection.label,
                sheet_number=detection.sheet_number,
                geometry_json=geometry_json,
                method=detection.method,
                confidence=detection.confidence,
                review_state=_review_state(
                    detection.confidence,
                    review_confidence_threshold,
                ),
                provenance_origin="observed",
                evidence_region_ids=detection.evidence_region_ids,
            )
        )

    for detection in connector_detections:
        geometry_json = json.dumps(
            {
                "points": detection.points,
                "arrow_at_start": detection.arrow_at_start,
                "arrow_at_end": detection.arrow_at_end,
            },
            separators=(",", ":"),
        )
        observations.append(
            DrawingObservation(
                observation_id=make_id(
                    "dobs",
                    f"{detection.sheet_number}:connector:{geometry_json}",
                ),
                observation_type="connector",
                sheet_number=detection.sheet_number,
                geometry_json=geometry_json,
                method=detection.method,
                confidence=detection.confidence,
                review_state=_review_state(
                    detection.confidence,
                    review_confidence_threshold,
                ),
                provenance_origin="observed",
            )
        )
    return observations


def _observation_center(
    observation: DrawingObservation,
) -> tuple[float, float] | None:
    geometry = observation.geometry
    bbox = geometry.get("bbox")
    if not bbox or len(bbox) != 4:
        return None
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


def _nearest_observation(
    point: tuple[float, float],
    observations: list[DrawingObservation],
    *,
    max_distance: float,
) -> DrawingObservation | None:
    candidates: list[tuple[float, DrawingObservation]] = []
    for observation in observations:
        center = _observation_center(observation)
        if center is None:
            continue
        distance = math.dist(point, center)
        if distance <= max_distance:
            candidates.append((distance, observation))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1].observation_id))[1]


def build_topology_candidates(
    observations: Iterable[DrawingObservation],
    *,
    endpoint_tolerance: float = 80.0,
    review_confidence_threshold: float = 0.65,
) -> list[DrawingTopologyCandidate]:
    """Infer endpoint relationships from observed connector geometry."""
    all_observations = list(observations)
    endpoints = [
        observation
        for observation in all_observations
        if observation.observation_type != "connector"
    ]
    topology: list[DrawingTopologyCandidate] = []

    for connector in (
        observation
        for observation in all_observations
        if observation.observation_type == "connector"
    ):
        geometry = connector.geometry
        points = [tuple(point) for point in geometry.get("points", [])]
        if len(points) < 2:
            continue
        same_sheet = [
            observation
            for observation in endpoints
            if observation.sheet_number == connector.sheet_number
        ]
        start_observation = _nearest_observation(
            points[0],
            same_sheet,
            max_distance=endpoint_tolerance,
        )
        end_observation = _nearest_observation(
            points[-1],
            same_sheet,
            max_distance=endpoint_tolerance,
        )
        arrow_start = bool(geometry.get("arrow_at_start"))
        arrow_end = bool(geometry.get("arrow_at_end"))
        relationship_type = "flows_to" if arrow_start or arrow_end else "connects_to"
        source_observation = start_observation
        target_observation = end_observation
        if arrow_start and not arrow_end:
            source_observation, target_observation = end_observation, start_observation
        confidence = connector.confidence
        if source_observation is None or target_observation is None:
            confidence = min(confidence, 0.5)
        topology.append(
            DrawingTopologyCandidate(
                topology_id=make_id(
                    "dtop",
                    f"{connector.observation_id}:{relationship_type}:"
                    f"{source_observation.observation_id if source_observation else ''}:"
                    f"{target_observation.observation_id if target_observation else ''}",
                ),
                relationship_type=relationship_type,
                source_observation_id=(
                    source_observation.observation_id
                    if source_observation
                    else None
                ),
                target_observation_id=(
                    target_observation.observation_id
                    if target_observation
                    else None
                ),
                connector_observation_id=connector.observation_id,
                sheet_number=connector.sheet_number,
                geometry_json=connector.geometry_json,
                method=f"endpoint_match:{connector.method}",
                confidence=confidence,
                review_state=_review_state(
                    confidence,
                    review_confidence_threshold,
                ),
                dangling=source_observation is None or target_observation is None,
            )
        )
    return topology


def drawing_observations_to_elements(
    source_file_id: str,
    observations: Iterable[DrawingObservation],
    *,
    project_id: str = "default",
    asset_id: str = "",
    asset_version_id: str = "",
    run_id: str = "",
    domain_hash: str | None = None,
    extracted_at: datetime | None = None,
) -> list[DocumentElementRow]:
    """Project drawing observations into the canonical document-element stream."""
    extracted_at = extracted_at or datetime.now(timezone.utc)
    elements: list[DocumentElementRow] = []
    for sort_order, observation in enumerate(observations):
        content = observation.label or observation.observation_type
        value_hash = compute_content_hash(
            f"{content}:{observation.geometry_json}:{observation.method}"
        )
        element_type = f"drawing_{observation.observation_type}"
        element_id = make_document_element_id(
            source_file_id,
            element_type,
            observation.sheet_number,
            sort_order,
            value_hash,
        )
        elements.append(
            DocumentElementRow(
                document_element_id=element_id,
                source_file_id=source_file_id,
                element_type=element_type,
                content=content,
                page_number=observation.sheet_number,
                sort_order=sort_order,
                content_hash=value_hash,
                extracted_at=extracted_at,
                project_id=project_id,
                asset_id=asset_id,
                asset_version_id=asset_version_id,
                run_id=run_id,
                parent_record_id=observation.observation_id,
                source_locator_json=json.dumps(
                    {
                        "drawing_observation_id": observation.observation_id,
                        "geometry": observation.geometry,
                        "method": observation.method,
                        "confidence": observation.confidence,
                        "review_state": observation.review_state,
                        "provenance_origin": observation.provenance_origin,
                        "evidence_region_ids": observation.evidence_region_ids,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                domain_hash=domain_hash,
            )
        )
    return elements
