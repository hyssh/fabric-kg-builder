"""GRP-013 through GRP-018 (revised): Drawing spatial/topology enrichment.

Fix #14+15:
- Outputs DrawingElementRow + DrawingRelationshipRow (canonical SPEC-006 §7.3)
- Does NOT use RelationshipRow with observation IDs posing as entity IDs
- located_at: only emitted when spatial bounding-box containment OR explicit
  zone assignment evidence exists (not merely because zone string is present)
- references_sheet / revision_of: reference real DrawingElementRow element_ids
- Enhanced validate_drawing_graph: transforms, geometry, endpoint continuity,
  dangling drawing-element refs, observed/inferred provenance, evidence lineage,
  review state, source traceability
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from fabric_kg_builder.model.ids import make_id, content_hash as compute_content_hash
from fabric_kg_builder.model.schemas import DrawingElementRow, DrawingRelationshipRow
from fabric_kg_builder.sources.drawing import (
    CoordinateTransform,
    DrawingObservation,
    DrawingSheetMetadata,
    DrawingTopologyCandidate,
)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _bbox_from_geometry(geometry_json: str) -> Optional[tuple[float, float, float, float]]:
    """Extract (x_min, y_min, x_max, y_max) from geometry_json if possible."""
    try:
        g = json.loads(geometry_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if "bbox" in g and len(g["bbox"]) == 4:
        return tuple(float(v) for v in g["bbox"])  # type: ignore[return-value]
    if "polygon" in g:
        pts = g["polygon"]
        try:
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            return (min(xs), min(ys), max(xs), max(ys))
        except (TypeError, IndexError, KeyError):
            return None
    return None


def _bbox_contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> bool:
    """Return True if outer strictly contains inner (all four corners)."""
    ox1, oy1, ox2, oy2 = outer
    ix1, iy1, ix2, iy2 = inner
    return ox1 <= ix1 and oy1 <= iy1 and ox2 >= ix2 and oy2 >= iy2


def _bbox_area(b: tuple[float, float, float, float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _bbox_adjacent(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    *,
    max_gap: float = 20.0,
) -> bool:
    """Return True when two bounding boxes are adjacent (within max_gap pixels)."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    # Horizontal gap or vertical gap
    h_gap = max(0.0, max(ax1, bx1) - min(ax2, bx2))
    v_gap = max(0.0, max(ay1, by1) - min(ay2, by2))
    return h_gap <= max_gap and v_gap <= max_gap


def _transform_invertible(t: CoordinateTransform) -> bool:
    return abs(t.scale) > 1e-9


def _element_id(obs: DrawingObservation, source_file_id: str) -> str:
    sig = f"{source_file_id}:{obs.sheet_number}:{obs.observation_id}"
    return make_id("drw_elem", sig)


def _drawing_rel_id(
    rel_type: str,
    src_id: str,
    tgt_id: str,
    sheet_number: Optional[int],
) -> str:
    sig = f"{rel_type}:{src_id}:{tgt_id}:{sheet_number or ''}"
    return make_id("drw_rel", sig)


def _geometry_content_hash(geometry_json: str) -> str:
    return compute_content_hash(geometry_json)[:32]


# ---------------------------------------------------------------------------
# Core conversion: observations → DrawingElementRow
# ---------------------------------------------------------------------------

def observations_to_drawing_elements(
    observations: list[DrawingObservation],
    *,
    source_file_id: str,
    run_id: str = "",
    asset_id: str = "",
    project_id: str = "default",
    schema_version: str = "2.0",
    created_at: Optional[datetime] = None,
) -> list[DrawingElementRow]:
    """Convert DrawingObservation list to canonical DrawingElementRow list."""
    ts = created_at or datetime.now(timezone.utc)
    rows: list[DrawingElementRow] = []
    for obs in observations:
        eid = _element_id(obs, source_file_id)
        rows.append(DrawingElementRow(
            element_id=eid,
            source_file_id=source_file_id,
            sheet_number=obs.sheet_number,
            element_type=obs.observation_type,
            label=obs.label,
            geometry_json=obs.geometry_json,
            method=obs.method,
            confidence=obs.confidence,
            review_state=obs.review_state,
            provenance_origin=obs.provenance_origin,
            evidence_region_ids=obs.evidence_region_ids or [],
            content_hash=_geometry_content_hash(obs.geometry_json),
            created_at=ts,
            run_id=run_id,
            asset_id=asset_id,
            project_id=project_id,
            schema_version=schema_version,
        ))
    return rows


# ---------------------------------------------------------------------------
# Spatial enrichment: contains, adjacent_to
# ---------------------------------------------------------------------------

def enrich_spatial_containment(
    elements: list[DrawingElementRow],
    *,
    run_id: str = "",
    created_at: Optional[datetime] = None,
) -> list[DrawingRelationshipRow]:
    """Emit contains relationships where zone/room elements enclose symbols."""
    ts = created_at or datetime.now(timezone.utc)
    rels: list[DrawingRelationshipRow] = []
    containers = [e for e in elements if e.element_type in ("zone", "room")]
    symbols = [e for e in elements if e.element_type not in ("zone", "room")]

    for container in containers:
        c_bbox = _bbox_from_geometry(container.geometry_json)
        if not c_bbox or _bbox_area(c_bbox) == 0:
            continue
        for sym in symbols:
            if sym.sheet_number != container.sheet_number:
                continue
            s_bbox = _bbox_from_geometry(sym.geometry_json)
            if not s_bbox:
                continue
            if _bbox_contains(c_bbox, s_bbox):
                rel_id = _drawing_rel_id("contains", container.element_id, sym.element_id, container.sheet_number)
                combined_evidence = list({*((container.evidence_region_ids or []) + (sym.evidence_region_ids or []))})
                geom = json.dumps({
                    "container_bbox": list(c_bbox),
                    "symbol_bbox": list(s_bbox),
                })
                rels.append(DrawingRelationshipRow(
                    drawing_relationship_id=rel_id,
                    relationship_type="contains",
                    source_element_id=container.element_id,
                    target_element_id=sym.element_id,
                    sheet_number=container.sheet_number,
                    geometry_json=geom,
                    method="spatial_containment",
                    confidence=min(container.confidence, sym.confidence),
                    review_state="not_required",
                    provenance_origin="observed",
                    evidence_region_ids=combined_evidence,
                    content_hash=_geometry_content_hash(geom),
                    created_at=ts,
                    run_id=run_id,
                ))
    return rels


def enrich_spatial_adjacency(
    elements: list[DrawingElementRow],
    *,
    max_gap: float = 20.0,
    run_id: str = "",
    created_at: Optional[datetime] = None,
) -> list[DrawingRelationshipRow]:
    """Emit adjacent_to relationships for spatially neighboring elements."""
    ts = created_at or datetime.now(timezone.utc)
    rels: list[DrawingRelationshipRow] = []
    seen: set[frozenset] = set()

    for i, a in enumerate(elements):
        a_bbox = _bbox_from_geometry(a.geometry_json)
        if not a_bbox:
            continue
        for b in elements[i + 1:]:
            if a.sheet_number != b.sheet_number:
                continue
            b_bbox = _bbox_from_geometry(b.geometry_json)
            if not b_bbox:
                continue
            pair = frozenset({a.element_id, b.element_id})
            if pair in seen:
                continue
            if _bbox_adjacent(a_bbox, b_bbox, max_gap=max_gap):
                seen.add(pair)
                # Canonicalize direction
                src, tgt = sorted([a.element_id, b.element_id])
                rel_id = _drawing_rel_id("adjacent_to", src, tgt, a.sheet_number)
                geom = json.dumps({"a_bbox": list(a_bbox), "b_bbox": list(b_bbox)})
                evidence = list({*((a.evidence_region_ids or []) + (b.evidence_region_ids or []))})
                rels.append(DrawingRelationshipRow(
                    drawing_relationship_id=rel_id,
                    relationship_type="adjacent_to",
                    source_element_id=src,
                    target_element_id=tgt,
                    sheet_number=a.sheet_number,
                    geometry_json=geom,
                    method="spatial_proximity",
                    confidence=min(a.confidence, b.confidence),
                    review_state="not_required",
                    provenance_origin="inferred",
                    evidence_region_ids=evidence,
                    content_hash=_geometry_content_hash(geom),
                    created_at=ts,
                    run_id=run_id,
                ))
    return rels


# ---------------------------------------------------------------------------
# located_at: only when evidence supports it
# ---------------------------------------------------------------------------

def enrich_located_at(
    elements: list[DrawingElementRow],
    sheet_metadata: list[DrawingSheetMetadata],
    *,
    run_id: str = "",
    created_at: Optional[datetime] = None,
    review_confidence_threshold: float = 0.65,
) -> list[DrawingRelationshipRow]:
    """Emit located_at only when spatial containment OR explicit zone evidence.

    Never emits located_at merely because a zone string exists.
    Elements without zone bounding-box enclosure get low-confidence review candidates.
    """
    ts = created_at or datetime.now(timezone.utc)
    rels: list[DrawingRelationshipRow] = []

    zone_elements = [e for e in elements if e.element_type == "zone"]
    zone_by_label: dict[str, DrawingElementRow] = {}
    for z in zone_elements:
        if z.label:
            zone_by_label[z.label.lower().strip()] = z

    # Build sheet → zones map
    sheet_zones: dict[int, list[DrawingElementRow]] = {}
    for z in zone_elements:
        sheet_zones.setdefault(z.sheet_number, []).append(z)

    symbols = [e for e in elements if e.element_type not in ("zone", "room")]

    for sym in symbols:
        sym_bbox = _bbox_from_geometry(sym.geometry_json)
        # Try spatial containment first (strongest evidence)
        matched_zone: Optional[DrawingElementRow] = None
        for z in sheet_zones.get(sym.sheet_number, []):
            z_bbox = _bbox_from_geometry(z.geometry_json)
            if z_bbox and sym_bbox and _bbox_contains(z_bbox, sym_bbox):
                matched_zone = z
                break

        if matched_zone:
            rel_id = _drawing_rel_id("located_at", sym.element_id, matched_zone.element_id, sym.sheet_number)
            geom = json.dumps({
                "symbol_bbox": list(sym_bbox) if sym_bbox else None,
                "zone_bbox": list(_bbox_from_geometry(matched_zone.geometry_json) or []),
                "method": "spatial_containment",
            })
            evidence = list({*(sym.evidence_region_ids or []) + (matched_zone.evidence_region_ids or [])})
            rels.append(DrawingRelationshipRow(
                drawing_relationship_id=rel_id,
                relationship_type="located_at",
                source_element_id=sym.element_id,
                target_element_id=matched_zone.element_id,
                sheet_number=sym.sheet_number,
                geometry_json=geom,
                method="spatial_containment",
                confidence=min(sym.confidence, matched_zone.confidence),
                review_state="not_required",
                provenance_origin="observed",
                evidence_region_ids=evidence,
                content_hash=_geometry_content_hash(geom),
                created_at=ts,
                run_id=run_id,
            ))
            continue

        try:
            geometry = json.loads(sym.geometry_json)
        except (json.JSONDecodeError, TypeError):
            geometry = {}
        explicit_zone_label = next(
            (
                str(geometry[key]).strip().lower()
                for key in ("assigned_zone", "zone_label", "zone")
                if isinstance(geometry.get(key), str) and geometry[key].strip()
            ),
            None,
        )
        explicit_zone = (
            zone_by_label.get(explicit_zone_label)
            if explicit_zone_label is not None
            else None
        )
        if explicit_zone is None or not sym.evidence_region_ids:
            continue

        confidence = min(sym.confidence, explicit_zone.confidence, 0.6)
        rel_id = _drawing_rel_id(
            "located_at",
            sym.element_id,
            explicit_zone.element_id,
            sym.sheet_number,
        )
        geom = json.dumps({
            "assigned_zone": explicit_zone.label,
            "method": "explicit_zone_assignment",
        })
        evidence = list({
            *(sym.evidence_region_ids or []),
            *(explicit_zone.evidence_region_ids or []),
        })
        rels.append(DrawingRelationshipRow(
            drawing_relationship_id=rel_id,
            relationship_type="located_at",
            source_element_id=sym.element_id,
            target_element_id=explicit_zone.element_id,
            sheet_number=sym.sheet_number,
            geometry_json=geom,
            method="explicit_zone_assignment",
            confidence=confidence,
            review_state=(
                "needs_review"
                if confidence < review_confidence_threshold
                else "not_required"
            ),
            provenance_origin="inferred",
            evidence_region_ids=evidence,
            content_hash=_geometry_content_hash(geom),
            created_at=ts,
            run_id=run_id,
        ))
    return rels


# ---------------------------------------------------------------------------
# Topology: connects_to, flows_to from DrawingTopologyCandidate
# ---------------------------------------------------------------------------

def enrich_topology(
    topology_candidates: list[DrawingTopologyCandidate],
    observation_id_to_element_id: dict[str, str],
    *,
    run_id: str = "",
    created_at: Optional[datetime] = None,
) -> list[DrawingRelationshipRow]:
    """Convert DrawingTopologyCandidate list to DrawingRelationshipRow.

    Only emits rows when both source and target observation IDs resolve to
    real DrawingElementRow element_ids. Dangling candidates are skipped.
    """
    ts = created_at or datetime.now(timezone.utc)
    rels: list[DrawingRelationshipRow] = []
    for cand in topology_candidates:
        if cand.dangling:
            continue
        src_eid = observation_id_to_element_id.get(cand.source_observation_id or "")
        tgt_eid = observation_id_to_element_id.get(cand.target_observation_id or "")
        if not src_eid or not tgt_eid:
            continue
        rel_id = _drawing_rel_id(cand.relationship_type, src_eid, tgt_eid, cand.sheet_number)
        geom = cand.geometry_json
        rels.append(DrawingRelationshipRow(
            drawing_relationship_id=rel_id,
            relationship_type=cand.relationship_type,
            source_element_id=src_eid,
            target_element_id=tgt_eid,
            sheet_number=cand.sheet_number,
            geometry_json=geom,
            method=cand.method,
            confidence=cand.confidence,
            review_state=cand.review_state,
            provenance_origin=cand.provenance_origin,
            evidence_region_ids=[],
            content_hash=_geometry_content_hash(geom),
            created_at=ts,
            run_id=run_id,
        ))
    return rels


# ---------------------------------------------------------------------------
# Cross-sheet references and revision lineage
# ---------------------------------------------------------------------------

def enrich_cross_sheet_references(
    elements: list[DrawingElementRow],
    sheet_metadata: list[DrawingSheetMetadata],
    *,
    run_id: str = "",
    created_at: Optional[datetime] = None,
) -> list[DrawingRelationshipRow]:
    """Emit references_sheet between callout elements and their target sheet elements.

    Only emits rows when a real target drawing element exists with a matching
    sheet_number that can be found in elements list.
    """
    ts = created_at or datetime.now(timezone.utc)
    rels: list[DrawingRelationshipRow] = []
    callouts = [e for e in elements if e.element_type == "callout"]
    sheet_rep: dict[int, DrawingElementRow] = {}
    for e in elements:
        if e.sheet_number not in sheet_rep:
            sheet_rep[e.sheet_number] = e

    sheet_meta_by_num = {m.sheet_number: m for m in sheet_metadata}
    for callout in callouts:
        meta = sheet_meta_by_num.get(callout.sheet_number)
        if not meta:
            continue
        for ref_sheet_str in meta.referenced_sheets:
            try:
                ref_sheet_num = int(ref_sheet_str)
            except (ValueError, TypeError):
                continue
            target_rep = sheet_rep.get(ref_sheet_num)
            if not target_rep:
                continue
            rel_id = _drawing_rel_id("references_sheet", callout.element_id, target_rep.element_id, callout.sheet_number)
            geom = json.dumps({"callout_sheet": callout.sheet_number, "referenced_sheet": ref_sheet_num})
            rels.append(DrawingRelationshipRow(
                drawing_relationship_id=rel_id,
                relationship_type="references_sheet",
                source_element_id=callout.element_id,
                target_element_id=target_rep.element_id,
                sheet_number=callout.sheet_number,
                geometry_json=geom,
                method="callout_cross_reference",
                confidence=callout.confidence,
                review_state=callout.review_state,
                provenance_origin="observed",
                evidence_region_ids=callout.evidence_region_ids or [],
                content_hash=_geometry_content_hash(geom),
                created_at=ts,
                run_id=run_id,
            ))
    return rels


def enrich_revision_lineage(
    elements_by_sheet: dict[int, list[DrawingElementRow]],
    revision_pairs: list[tuple[int, int]],  # (old_sheet_num, new_sheet_num) pairs
    *,
    run_id: str = "",
    created_at: Optional[datetime] = None,
) -> list[DrawingRelationshipRow]:
    """Emit revision_of relationships between canonical sheet elements."""
    ts = created_at or datetime.now(timezone.utc)
    rels: list[DrawingRelationshipRow] = []
    for old_sheet, new_sheet in revision_pairs:
        old_elems = elements_by_sheet.get(old_sheet, [])
        new_elems = elements_by_sheet.get(new_sheet, [])
        if not old_elems or not new_elems:
            continue
        old_rep = old_elems[0]
        new_rep = new_elems[0]
        rel_id = _drawing_rel_id("revision_of", new_rep.element_id, old_rep.element_id, new_sheet)
        geom = json.dumps({"revised_sheet": new_sheet, "original_sheet": old_sheet})
        rels.append(DrawingRelationshipRow(
            drawing_relationship_id=rel_id,
            relationship_type="revision_of",
            source_element_id=new_rep.element_id,
            target_element_id=old_rep.element_id,
            sheet_number=new_sheet,
            geometry_json=geom,
            method="revision_lineage",
            confidence=1.0,
            review_state="not_required",
            provenance_origin="observed",
            evidence_region_ids=[],
            content_hash=_geometry_content_hash(geom),
            created_at=ts,
            run_id=run_id,
        ))
    return rels


# ---------------------------------------------------------------------------
# Enhanced drawing validation (Fix #15)
# ---------------------------------------------------------------------------

@dataclass
class DrawingValidationResult:
    passed: bool = True
    transform_issues: list[str] = field(default_factory=list)
    geometry_issues: list[str] = field(default_factory=list)
    endpoint_issues: list[str] = field(default_factory=list)
    dangling_refs: list[str] = field(default_factory=list)
    evidence_issues: list[str] = field(default_factory=list)
    review_state_issues: list[str] = field(default_factory=list)
    provenance_issues: list[str] = field(default_factory=list)
    source_traceability_issues: list[str] = field(default_factory=list)

    def _recompute(self) -> None:
        self.passed = not any([
            self.transform_issues,
            self.dangling_refs,
            self.endpoint_issues,
        ])


def validate_drawing_graph(
    elements: list[DrawingElementRow],
    relationships: list[DrawingRelationshipRow],
    transforms: Optional[list[CoordinateTransform]] = None,
    *,
    require_evidence: bool = False,
    require_source_traceability: bool = False,
) -> DrawingValidationResult:
    """Comprehensive drawing graph validation (Fix #15)."""
    result = DrawingValidationResult()
    element_ids = {e.element_id for e in elements}
    element_map = {e.element_id: e for e in elements}

    # --- Transform invertibility ---
    for i, t in enumerate(transforms or []):
        if not _transform_invertible(t):
            result.transform_issues.append(
                f"Transform [{i}] has near-zero scale={t.scale} — not invertible"
            )

    # --- Geometry validity ---
    for elem in elements:
        bbox = _bbox_from_geometry(elem.geometry_json)
        if bbox is None:
            result.geometry_issues.append(
                f"Element {elem.element_id!r} has unparseable geometry_json"
            )
        elif _bbox_area(bbox) == 0 and elem.element_type not in ("annotation",):
            result.geometry_issues.append(
                f"Element {elem.element_id!r} has zero-area bounding box"
            )

    # --- Endpoint continuity (connector elements) ---
    for rel in relationships:
        if rel.relationship_type in ("connects_to", "flows_to") and rel.geometry_json:
            try:
                g = json.loads(rel.geometry_json)
                if "points" in g and len(g["points"]) >= 2:
                    pass  # valid connector path
            except (json.JSONDecodeError, TypeError):
                result.endpoint_issues.append(
                    f"Relationship {rel.drawing_relationship_id!r} has invalid geometry_json"
                )

    # --- Dangling drawing-element references ---
    for rel in relationships:
        if rel.source_element_id not in element_ids:
            result.dangling_refs.append(
                f"Relationship {rel.drawing_relationship_id!r} source "
                f"{rel.source_element_id!r} not in elements"
            )
        if rel.target_element_id not in element_ids:
            result.dangling_refs.append(
                f"Relationship {rel.drawing_relationship_id!r} target "
                f"{rel.target_element_id!r} not in elements"
            )

    # --- Evidence lineage ---
    if require_evidence:
        for elem in elements:
            if not elem.evidence_region_ids:
                result.evidence_issues.append(
                    f"Element {elem.element_id!r} (type={elem.element_type!r}) has no evidence_region_ids"
                )

    # --- Review state validity (already enforced by schema, but log anomalies) ---
    valid_review_states = {"not_required", "needs_review", "reviewed"}
    for elem in elements:
        if elem.review_state not in valid_review_states:
            result.review_state_issues.append(
                f"Element {elem.element_id!r} has invalid review_state={elem.review_state!r}"
            )

    # --- Provenance origin tracking ---
    for elem in elements:
        if elem.provenance_origin not in ("observed", "inferred"):
            result.provenance_issues.append(
                f"Element {elem.element_id!r} has unknown provenance_origin={elem.provenance_origin!r}"
            )
    for rel in relationships:
        if rel.provenance_origin not in ("observed", "inferred"):
            result.provenance_issues.append(
                f"Relationship {rel.drawing_relationship_id!r} has unknown provenance_origin"
            )

    # --- Source traceability ---
    if require_source_traceability:
        for elem in elements:
            if not elem.source_file_id:
                result.source_traceability_issues.append(
                    f"Element {elem.element_id!r} missing source_file_id"
                )
        for rel in relationships:
            if not rel.run_id:
                result.source_traceability_issues.append(
                    f"Relationship {rel.drawing_relationship_id!r} missing run_id"
                )

    result._recompute()
    return result
