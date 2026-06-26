"""Data-integrity validation gates VAL-001..VAL-012.

Implements the subset of SPEC-002 §9 validation rules that operate on
in-memory row dicts (before and after Parquet write).

Gates
-----
VAL-001  No duplicate entity_id values in entities            (D-09)
VAL-002  No duplicate relationship_id values in relationships (D-10)
VAL-003  No duplicate chunk_id values in chunks               (D-11)
VAL-004  No duplicate evidence_id values in evidence          (D-12)
VAL-005  relationships.source_entity_id exists in entities    (D-02 a)
VAL-006  relationships.target_entity_id exists in entities    (D-02 b)
VAL-007  relationships.evidence_id (non-null) exists in evidence (D-03)
VAL-008  No duplicate image_id values in visual_assets        (D-13)
VAL-009  No duplicate visual_region_id values in visual_regions (D-14)
VAL-010  visual_regions.image_id exists in visual_assets      (D-05 for visual_regions)
VAL-011  evidence.image_id (non-null) exists in visual_assets (D-05 for evidence)
VAL-012  evidence.visual_region_id / callout_id (non-null) exist in visual_regions (D-06)

Usage
-----
    from fabric_kg_builder.validate.data_gates import run_gates, Violation

    violations = run_gates(table_rows)
    if violations:
        for v in violations:
            print(v)
        sys.exit(5)

``table_rows`` is a ``dict[str, list[dict]]`` mapping canonical table names
to their row dicts.  Tables absent from the dict are treated as empty.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Violation record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """A single data-integrity violation."""

    gate: str     # e.g. "VAL-001"
    table: str    # affected table name
    message: str  # human-readable description

    def __str__(self) -> str:
        return f"[{self.gate}] {self.table}: {self.message}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_gates(table_rows: dict[str, list[dict]]) -> list[Violation]:
    """Run VAL-001..VAL-012 against *table_rows*.

    Parameters
    ----------
    table_rows:
        Mapping of canonical table name → list of row dicts.  Missing tables
        are treated as empty lists.

    Returns
    -------
    list[Violation]
        All violations found.  Empty list means the data passed all gates.
    """
    violations: list[Violation] = []
    violations.extend(_val001_dup_entity_ids(table_rows))
    violations.extend(_val002_dup_relationship_ids(table_rows))
    violations.extend(_val003_dup_chunk_ids(table_rows))
    violations.extend(_val004_dup_evidence_ids(table_rows))
    violations.extend(_val005_dangling_source_entity(table_rows))
    violations.extend(_val006_dangling_target_entity(table_rows))
    violations.extend(_val007_dangling_evidence_fk(table_rows))
    violations.extend(_val008_dup_image_ids(table_rows))
    violations.extend(_val009_dup_visual_region_ids(table_rows))
    violations.extend(_val010_visual_regions_image_fk(table_rows))
    violations.extend(_val011_evidence_image_fk(table_rows))
    violations.extend(_val012_evidence_visual_region_fk(table_rows))
    return violations


# ---------------------------------------------------------------------------
# Individual gate implementations
# ---------------------------------------------------------------------------


def _dup_id_violations(
    rows: list[dict],
    id_field: str,
    gate: str,
    table: str,
) -> list[Violation]:
    ids = [r[id_field] for r in rows if r.get(id_field) is not None]
    counts = Counter(ids)
    return [
        Violation(
            gate, table,
            f"Duplicate {id_field} '{id_val}' appears {count} time(s)",
        )
        for id_val, count in counts.items()
        if count > 1
    ]


def _val001_dup_entity_ids(table_rows: dict[str, list[dict]]) -> list[Violation]:
    return _dup_id_violations(
        table_rows.get("entities", []), "entity_id", "VAL-001", "entities"
    )


def _val002_dup_relationship_ids(table_rows: dict[str, list[dict]]) -> list[Violation]:
    return _dup_id_violations(
        table_rows.get("relationships", []),
        "relationship_id",
        "VAL-002",
        "relationships",
    )


def _val003_dup_chunk_ids(table_rows: dict[str, list[dict]]) -> list[Violation]:
    return _dup_id_violations(
        table_rows.get("chunks", []), "chunk_id", "VAL-003", "chunks"
    )


def _val004_dup_evidence_ids(table_rows: dict[str, list[dict]]) -> list[Violation]:
    return _dup_id_violations(
        table_rows.get("evidence", []), "evidence_id", "VAL-004", "evidence"
    )


def _val005_dangling_source_entity(table_rows: dict[str, list[dict]]) -> list[Violation]:
    entity_ids = {
        r["entity_id"]
        for r in table_rows.get("entities", [])
        if r.get("entity_id") is not None
    }
    violations: list[Violation] = []
    for rel in table_rows.get("relationships", []):
        sid = rel.get("source_entity_id")
        if sid is not None and sid not in entity_ids:
            violations.append(
                Violation(
                    "VAL-005",
                    "relationships",
                    f"source_entity_id '{sid}' not found in entities",
                )
            )
    return violations


def _val006_dangling_target_entity(table_rows: dict[str, list[dict]]) -> list[Violation]:
    entity_ids = {
        r["entity_id"]
        for r in table_rows.get("entities", [])
        if r.get("entity_id") is not None
    }
    violations: list[Violation] = []
    for rel in table_rows.get("relationships", []):
        tid = rel.get("target_entity_id")
        if tid is not None and tid not in entity_ids:
            violations.append(
                Violation(
                    "VAL-006",
                    "relationships",
                    f"target_entity_id '{tid}' not found in entities",
                )
            )
    return violations


def _val007_dangling_evidence_fk(table_rows: dict[str, list[dict]]) -> list[Violation]:
    evidence_ids = {
        r["evidence_id"]
        for r in table_rows.get("evidence", [])
        if r.get("evidence_id") is not None
    }
    violations: list[Violation] = []
    for rel in table_rows.get("relationships", []):
        eid = rel.get("evidence_id")
        if eid is not None and eid not in evidence_ids:
            violations.append(
                Violation(
                    "VAL-007",
                    "relationships",
                    f"evidence_id '{eid}' not found in evidence",
                )
            )
    return violations


def _val008_dup_image_ids(table_rows: dict[str, list[dict]]) -> list[Violation]:
    return _dup_id_violations(
        table_rows.get("visual_assets", []), "image_id", "VAL-008", "visual_assets"
    )


def _val009_dup_visual_region_ids(table_rows: dict[str, list[dict]]) -> list[Violation]:
    return _dup_id_violations(
        table_rows.get("visual_regions", []),
        "visual_region_id",
        "VAL-009",
        "visual_regions",
    )


def _val010_visual_regions_image_fk(table_rows: dict[str, list[dict]]) -> list[Violation]:
    """VAL-010: visual_regions.image_id must exist in visual_assets (D-05)."""
    image_ids = {
        r["image_id"]
        for r in table_rows.get("visual_assets", [])
        if r.get("image_id") is not None
    }
    violations: list[Violation] = []
    for vr in table_rows.get("visual_regions", []):
        iid = vr.get("image_id")
        if iid is not None and iid not in image_ids:
            violations.append(
                Violation(
                    "VAL-010",
                    "visual_regions",
                    f"image_id '{iid}' not found in visual_assets",
                )
            )
    return violations


def _val011_evidence_image_fk(table_rows: dict[str, list[dict]]) -> list[Violation]:
    """VAL-011: evidence.image_id (non-null) must exist in visual_assets (D-05)."""
    image_ids = {
        r["image_id"]
        for r in table_rows.get("visual_assets", [])
        if r.get("image_id") is not None
    }
    violations: list[Violation] = []
    for ev in table_rows.get("evidence", []):
        iid = ev.get("image_id")
        if iid is not None and iid not in image_ids:
            violations.append(
                Violation(
                    "VAL-011",
                    "evidence",
                    f"image_id '{iid}' not found in visual_assets",
                )
            )
    return violations


def _val012_evidence_visual_region_fk(table_rows: dict[str, list[dict]]) -> list[Violation]:
    """VAL-012: evidence.visual_region_id / callout_id (non-null) must exist in visual_regions (D-06)."""
    vr_ids = {
        r["visual_region_id"]
        for r in table_rows.get("visual_regions", [])
        if r.get("visual_region_id") is not None
    }
    violations: list[Violation] = []
    for ev in table_rows.get("evidence", []):
        for col in ("visual_region_id", "callout_id"):
            vrid = ev.get(col)
            if vrid is not None and vrid not in vr_ids:
                violations.append(
                    Violation(
                        "VAL-012",
                        "evidence",
                        f"{col} '{vrid}' not found in visual_regions",
                    )
                )
    return violations
