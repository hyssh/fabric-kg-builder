from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from fabric_kg_builder.sources.drawing import (
    ConnectorDetection,
    DetectorObservation,
    DrawingConfig,
    DrawingLimitError,
    DrawingTextRegion,
    build_topology_candidates,
    drawing_tile_filename,
    drawing_observations_to_elements,
    extract_drawing_observations,
    extract_sheet_metadata,
    tile_drawing,
)


def _region(
    region_id: str,
    text: str,
    bbox: tuple[float, float, float, float],
    confidence: float = 0.95,
) -> DrawingTextRegion:
    x0, y0, x1, y1 = bbox
    return DrawingTextRegion(
        region_id=region_id,
        text=text,
        polygon=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
        confidence=confidence,
    )


def test_drawing_tiles_include_preview_pyramid_and_roundtrip(tmp_path: Path) -> None:
    source = tmp_path / "architecture-blueprint.png"
    image = Image.new("RGB", (2_200, 1_400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 2_000, 1_200), outline="black", width=6)
    image.save(source)

    tiles = tile_drawing(
        source,
        config=DrawingConfig(
            tile_size=1_024,
            tile_overlap_ratio=0.05,
            pyramid_scales=(1.0, 0.5),
            preview_max_dimension=1_000,
        ),
    )

    assert tiles[0].kind == "full_sheet"
    assert {tile.level for tile in tiles if tile.kind == "tile"} == {0, 1}
    level_zero = [tile for tile in tiles if tile.kind == "tile" and tile.level == 0]
    assert len(level_zero) >= 6

    tile = next(item for item in level_zero if item.transform.origin_x > 0)
    original = tile.transform.tile_to_original(125, 75)
    restored = tile.transform.original_to_tile(*original)
    assert restored == pytest.approx((125, 75))


def test_drawing_tile_filenames_are_cross_platform_safe(tmp_path: Path) -> None:
    filenames: set[str] = set()
    for index in range(3_000):
        tile_id = f"dtile:{index:032x}"
        filename = drawing_tile_filename(tile_id)
        assert ":" not in filename
        assert filename not in filenames
        filenames.add(filename)
        (tmp_path / filename).write_bytes(b"png")

    assert len(list(tmp_path.glob("*.png"))) == 3_000


def test_drawing_tiling_enforces_large_sheet_limit(tmp_path: Path) -> None:
    source = tmp_path / "oversized.png"
    Image.new("RGB", (1_200, 1_200), "white").save(source)

    with pytest.raises(DrawingLimitError, match="limit"):
        tile_drawing(
            source,
            config=DrawingConfig(
                tile_size=512,
                pyramid_scales=(1.0,),
                max_pixels_per_sheet=1_000_000,
            ),
        )


def test_title_block_metadata_keeps_region_evidence() -> None:
    regions = [
        _region("title", "TITLE: Cooling Water P&ID", (650, 700, 980, 740)),
        _region("drawing", "DRAWING NO: PID-204", (650, 750, 980, 790)),
        _region("revision", "REV: C", (650, 800, 760, 840)),
        _region("scale", "SCALE: 1:50", (760, 800, 880, 840)),
        _region("units", "UNITS: mm", (880, 800, 990, 840)),
        _region("discipline", "DISCIPLINE: Process", (650, 850, 990, 890)),
        _region("legend", "LEGEND: XV isolation valve", (50, 100, 350, 150)),
        _region("note", "NOTE 1: Verify flow direction", (50, 180, 400, 220)),
        _region("reference", "SEE SHEET PID-205 ZONE B4", (50, 250, 400, 290)),
    ]

    metadata = extract_sheet_metadata(
        regions,
        sheet_number=1,
        sheet_width=1_000,
        sheet_height=1_000,
    )

    assert metadata.title == "Cooling Water P&ID"
    assert metadata.drawing_id == "PID-204"
    assert metadata.revision == "C"
    assert metadata.scale_text == "1:50"
    assert metadata.units_text == "mm"
    assert metadata.discipline == "Process"
    assert metadata.referenced_sheets == ["PID-205"]
    assert metadata.zones == ["B4"]
    assert metadata.evidence_region_ids["drawing_id"] == ["drawing"]


def test_symbol_connector_and_callout_candidates_keep_observed_provenance() -> None:
    text_regions = [
        _region("callout-p101", "P-101", (80, 90, 130, 120)),
        _region("dimension", "25 mm", (300, 300, 360, 330), confidence=0.55),
    ]
    symbols = [
        DetectorObservation(
            label="Pump",
            observation_type="symbol",
            bbox=(100, 100, 180, 180),
            method="vision-model-v1",
            confidence=0.92,
            evidence_region_ids=["callout-p101"],
        ),
        DetectorObservation(
            label="Tank",
            observation_type="symbol",
            bbox=(500, 100, 600, 220),
            method="vision-model-v1",
            confidence=0.9,
        ),
    ]
    connectors = [
        ConnectorDetection(
            points=[(150, 140), (550, 160)],
            arrow_at_end=True,
            confidence=0.88,
        )
    ]

    observations = extract_drawing_observations(
        text_regions,
        symbol_detections=symbols,
        connector_detections=connectors,
    )
    topology = build_topology_candidates(
        observations,
        endpoint_tolerance=100,
    )

    dimension = next(
        item for item in observations if item.observation_type == "dimension"
    )
    assert dimension.review_state == "needs_review"
    assert all(item.provenance_origin == "observed" for item in observations)
    assert len(topology) == 1
    assert topology[0].relationship_type == "flows_to"
    assert topology[0].source_observation_id is not None
    assert topology[0].target_observation_id is not None
    assert topology[0].provenance_origin == "inferred"
    assert topology[0].dangling is False

    elements = drawing_observations_to_elements(
        "src:drawing-1",
        observations,
        asset_id="asset-1",
        asset_version_id="version-1",
        run_id="run-1",
    )
    assert len(elements) == len(observations)
    locator = json.loads(elements[0].source_locator_json or "{}")
    assert locator["provenance_origin"] == "observed"
    assert elements[0].asset_id == "asset-1"
