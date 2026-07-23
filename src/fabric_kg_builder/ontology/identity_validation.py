"""OKV-001 / OKV-002 validation gates for ontology identity integrity.

OKV-001: Relationship endpoint FK column must be compatible with the referenced
         entity's declared identity domain.  Fires ONTOLOGY_RELATIONSHIP_KEY_MISMATCH
         with actionable context naming the relationship, endpoint, actual column,
         and expected identity column.

OKV-002: Properties typed 'timestamp' whose name suggests partial-date data
         (contains the substring 'date') trigger PARTIAL_DATE_INCOMPATIBLE before
         deployment.  Use type 'string' to preserve year-only / year-month values
         losslessly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

@dataclass
class IdentityViolation:
    """A single OKV gate finding with gate identity, severity, and message."""

    gate_id: str
    severity: str   # "error" | "warning"
    message: str

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, IdentityViolation):
            return NotImplemented
        return (
            self.gate_id == other.gate_id
            and self.severity == other.severity
            and self.message == other.message
        )


class DatePrecision(Enum):
    """Coarseness levels for date/time values, ordered from coarsest to finest."""

    YEAR = "year"
    YEAR_MONTH = "year_month"
    FULL_DATE = "full_date"
    TIMESTAMP = "timestamp"
    UNKNOWN = "unknown"


# Coarseness ranking: lower index = coarser
_PRECISION_ORDER = [
    DatePrecision.YEAR,
    DatePrecision.YEAR_MONTH,
    DatePrecision.FULL_DATE,
    DatePrecision.TIMESTAMP,
]

_RE_YEAR = re.compile(r"^\d{4}$")
_RE_YEAR_MONTH = re.compile(r"^\d{4}-\d{2}$")
_RE_FULL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]")


# ---------------------------------------------------------------------------
# Public query helpers
# ---------------------------------------------------------------------------

def resolve_entity_identity_map(model: dict[str, Any]) -> dict[str, list[str]]:
    """Return entity_name → list-of-identity-column-names for every entity type.

    Resolution order:
    1. ``entityIdProperties`` list (multi-part key) — overrides single column.
    2. ``dataBinding.entityIdColumn`` — the primary physical identity column.
    3. Fallback to ``entity_id`` property when neither of the above is declared.
    """
    result: dict[str, list[str]] = {}
    for et in model.get("entityTypes", []):
        name = et.get("name", "")
        # 1. Explicit multi-part key list
        id_props = et.get("entityIdProperties")
        if id_props:
            result[name] = list(id_props)
            continue
        # 2. Single identity column from dataBinding
        binding = et.get("dataBinding", {})
        id_col = binding.get("entityIdColumn", "")
        if id_col:
            result[name] = [id_col]
            continue
        # 3. Fallback: entity_id property
        prop_names = _entity_property_names(et)
        result[name] = ["entity_id"] if "entity_id" in prop_names else []
    return result


def resolve_relationship_endpoint_map(
    model: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Return rel_name → {"source": source_col, "target": target_col}."""
    result: dict[str, dict[str, str]] = {}
    for rt in model.get("relationshipTypes", []):
        name = rt.get("name", "")
        binding = rt.get("dataBinding", {})
        result[name] = {
            "source": binding.get("sourceEntityIdColumn", ""),
            "target": binding.get("targetEntityIdColumn", ""),
        }
    return result


# ---------------------------------------------------------------------------
# Primary gate entry point
# ---------------------------------------------------------------------------

def validate_identity(model: dict[str, Any]) -> list[IdentityViolation]:
    """Run OKV-001 and OKV-002 gates.  Returns a list of IdentityViolation."""
    violations: list[IdentityViolation] = []
    violations.extend(_check_okv001(model))
    violations.extend(_check_okv002(model))
    return violations


# ---------------------------------------------------------------------------
# OKV-001 — relationship endpoint / entity identity alignment
# ---------------------------------------------------------------------------

def _entity_property_names(entity: dict[str, Any]) -> set[str]:
    return {p.get("name", "") for p in entity.get("properties", [])}


def _implied_domain(fk_col: str) -> str | None:
    """Strip leading source_/target_ prefix to derive the implied identity column.

    'source_entity_id' → 'entity_id'
    'target_chunk_id'  → 'chunk_id'
    'image_id'         → None  (no canonical prefix → FK must match exactly)
    """
    for prefix in ("source_", "target_"):
        if fk_col.startswith(prefix):
            return fk_col[len(prefix):]
    return None


def _is_endpoint_compatible(entity: dict[str, Any], fk_col: str) -> bool:
    """Return True when *fk_col* is a valid reference into *entity*'s identity domain.

    Compatibility rules (any one sufficient):
    1. FK column name exactly equals entityIdColumn (handles same-table bindings).
    2. The implied domain (strip source_/target_ prefix) equals entityIdColumn.
    3. The entity declares a property whose name equals the implied domain,
       meaning the entity explicitly exposes that identity alias.
    """
    binding = entity.get("dataBinding", {})
    id_col = binding.get("entityIdColumn", "")

    # Rule 1: exact match (e.g. stored_at uses image_id/image_id)
    if fk_col == id_col:
        return True

    implied = _implied_domain(fk_col)
    if implied is None:
        # No prefix to strip; FK must equal entityIdColumn exactly (already checked)
        return False

    # Rule 2: implied domain equals entity's identity column
    if id_col == implied:
        return True

    # Rule 3: entity has an explicit property matching the implied domain
    if implied in _entity_property_names(entity):
        return True

    return False


def _check_okv001(model: dict[str, Any]) -> list[IdentityViolation]:
    violations: list[IdentityViolation] = []
    entity_map = {et.get("name", ""): et for et in model.get("entityTypes", [])}

    for rt in model.get("relationshipTypes", []):
        binding = rt.get("dataBinding", {})
        rel_name = rt.get("name", "")
        src_type = rt.get("sourceType", "")
        tgt_type = rt.get("targetType", "")
        src_col = binding.get("sourceEntityIdColumn", "")
        tgt_col = binding.get("targetEntityIdColumn", "")

        src_entity = entity_map.get(src_type)
        tgt_entity = entity_map.get(tgt_type)

        if src_entity:
            if not src_col:
                violations.append(
                    IdentityViolation(
                        gate_id="OKV-001",
                        severity="error",
                        message=(
                            f"ONTOLOGY_RELATIONSHIP_KEY_MISMATCH: relationship '{rel_name}' "
                            f"is missing sourceEntityIdColumn in dataBinding. "
                            f"Cannot validate source endpoint for entity '{src_type}'."
                        ),
                    )
                )
            elif not _is_endpoint_compatible(src_entity, src_col):
                src_id_col = src_entity.get("dataBinding", {}).get("entityIdColumn", "?")
                violations.append(
                    IdentityViolation(
                        gate_id="OKV-001",
                        severity="error",
                        message=(
                            f"ONTOLOGY_RELATIONSHIP_KEY_MISMATCH: relationship '{rel_name}' "
                            f"source endpoint column '{src_col}' is incompatible with entity "
                            f"'{src_type}' identity column '{src_id_col}'. "
                            f"Add an 'entity_id' property alias to '{src_type}' or use a "
                            f"compatible FK column."
                        ),
                    )
                )

        if tgt_entity:
            if not tgt_col:
                violations.append(
                    IdentityViolation(
                        gate_id="OKV-001",
                        severity="error",
                        message=(
                            f"ONTOLOGY_RELATIONSHIP_KEY_MISMATCH: relationship '{rel_name}' "
                            f"is missing targetEntityIdColumn in dataBinding. "
                            f"Cannot validate target endpoint for entity '{tgt_type}'."
                        ),
                    )
                )
            elif not _is_endpoint_compatible(tgt_entity, tgt_col):
                tgt_id_col = tgt_entity.get("dataBinding", {}).get("entityIdColumn", "?")
                violations.append(
                    IdentityViolation(
                        gate_id="OKV-001",
                        severity="error",
                        message=(
                            f"ONTOLOGY_RELATIONSHIP_KEY_MISMATCH: relationship '{rel_name}' "
                            f"target endpoint column '{tgt_col}' is incompatible with entity "
                            f"'{tgt_type}' identity column '{tgt_id_col}'. "
                            f"Add an 'entity_id' property alias to '{tgt_type}' or use a "
                            f"compatible FK column."
                        ),
                    )
                )

    return violations


# ---------------------------------------------------------------------------
# OKV-002 — partial date / timestamp type mismatch
# ---------------------------------------------------------------------------

_RE_DATE_NAME = re.compile(r"date", re.IGNORECASE)


def _check_okv002(model: dict[str, Any]) -> list[IdentityViolation]:
    """Flag entity properties typed 'timestamp' whose name contains 'date'.

    Such properties likely hold partial-date data (year-only, year-month) that
    cannot be projected to Fabric DateTime without inventing precision.
    """
    violations: list[IdentityViolation] = []
    for et in model.get("entityTypes", []):
        entity_name = et.get("name", "")
        for prop in et.get("properties", []):
            prop_name = prop.get("name", "")
            prop_type = prop.get("type", "")
            if prop_type == "timestamp" and _RE_DATE_NAME.search(prop_name):
                violations.append(
                    IdentityViolation(
                        gate_id="OKV-002",
                        severity="error",
                        message=(
                            f"PARTIAL_DATE_INCOMPATIBLE: entity '{entity_name}' property "
                            f"'{prop_name}' is typed 'timestamp' but name suggests partial "
                            f"date data (year-only or year-month). Use type 'string' to "
                            f"preserve partial dates losslessly."
                        ),
                    )
                )
    return violations


# ---------------------------------------------------------------------------
# Date precision detection
# ---------------------------------------------------------------------------

def _classify_single(value: str) -> DatePrecision | None:
    """Classify a single string value; return None for unrecognised formats or non-strings."""
    if not isinstance(value, str):
        return None
    if _RE_YEAR.match(value):
        return DatePrecision.YEAR
    if _RE_YEAR_MONTH.match(value):
        return DatePrecision.YEAR_MONTH
    if _RE_FULL_DATE.match(value):
        return DatePrecision.FULL_DATE
    if _RE_TIMESTAMP.match(value):
        return DatePrecision.TIMESTAMP
    return None


def detect_date_precision(values: list[str]) -> DatePrecision:
    """Return the coarsest DatePrecision level observed in *values*.

    Coarseness order (coarsest first): YEAR → YEAR_MONTH → FULL_DATE → TIMESTAMP.
    Unrecognised strings are skipped.  Empty input or all-unrecognised → UNKNOWN.
    Input list is never modified.
    """
    coarsest: DatePrecision | None = None

    for val in values:
        precision = _classify_single(val)
        if precision is None:
            continue  # skip unrecognised
        if coarsest is None:
            coarsest = precision
        else:
            # Take the coarser (lower index in _PRECISION_ORDER)
            if _PRECISION_ORDER.index(precision) < _PRECISION_ORDER.index(coarsest):
                coarsest = precision

    return coarsest if coarsest is not None else DatePrecision.UNKNOWN


# ---------------------------------------------------------------------------
# Date property report
# ---------------------------------------------------------------------------

_RE_DATE_LIKE_NAME = re.compile(r"date|year|month", re.IGNORECASE)
_DATE_LIKE_TYPES = {"timestamp", "datetime", "date"}


def get_date_property_report(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one dict per date-bearing property across all entity types.

    A property is date-bearing when its declared type is timestamp/datetime/date
    or its name contains 'date', 'year', or 'month'.

    Each entry contains: entity_type, property_name, declared_type.
    """
    result: list[dict[str, Any]] = []
    for et in model.get("entityTypes", []):
        entity_name = et.get("name", "")
        for prop in et.get("properties", []):
            prop_name = prop.get("name", "")
            prop_type = prop.get("type", "")
            if prop_type in _DATE_LIKE_TYPES or _RE_DATE_LIKE_NAME.search(prop_name):
                result.append(
                    {
                        "entity_type": entity_name,
                        "property_name": prop_name,
                        "declared_type": prop_type,
                    }
                )
    return result


# ---------------------------------------------------------------------------
# Post-deploy structural validation
# ---------------------------------------------------------------------------

_RE_RT_ID = re.compile(r"RelationshipTypes/([^/]+)/")


def validate_post_deploy_definition(
    definition: dict[str, Any],
    model: dict[str, Any],
) -> list[IdentityViolation]:
    """Check that the ontology definition returned by Fabric is structurally complete.

    Validates:
    - At least one EntityType definition part per entity declared in the model.
    - At least one RelationshipType definition part per relationship in the model.
    - Every RelationshipType that has a definition part also has at least one
      Contextualization part (zero contextualizations = disconnected relationship).
    """
    violations: list[IdentityViolation] = []
    parts = definition.get("parts", [])

    entity_def_count = sum(
        1
        for p in parts
        if "EntityTypes/" in p.get("path", "")
        and "/definition.json" in p.get("path", "")
    )
    rel_def_ids: set[str] = set()
    rel_ctx_ids: set[str] = set()

    for p in parts:
        path = p.get("path", "")
        if "RelationshipTypes/" in path and "/definition.json" in path:
            m = _RE_RT_ID.search(path)
            if m:
                rel_def_ids.add(m.group(1))
        if "RelationshipTypes/" in path and "/Contextualizations/" in path:
            m = _RE_RT_ID.search(path)
            if m:
                rel_ctx_ids.add(m.group(1))

    model_entity_count = len(model.get("entityTypes", []))
    model_rel_count = len(model.get("relationshipTypes", []))

    # Entity count check
    if entity_def_count == 0:
        violations.append(
            IdentityViolation(
                gate_id="OKV-001",
                severity="error",
                message=(
                    f"Post-deploy definition contains zero EntityType entries; "
                    f"expected {model_entity_count}. Ontology may not have deployed correctly."
                ),
            )
        )
    elif entity_def_count < model_entity_count:
        violations.append(
            IdentityViolation(
                gate_id="OKV-001",
                severity="error",
                message=(
                    f"Post-deploy definition has {entity_def_count} EntityType entries "
                    f"but model declares {model_entity_count}. "
                    f"{model_entity_count - entity_def_count} entity type(s) missing."
                ),
            )
        )

    # Relationship count check
    if model_rel_count > 0 and len(rel_def_ids) < model_rel_count:
        violations.append(
            IdentityViolation(
                gate_id="OKV-001",
                severity="error",
                message=(
                    f"Post-deploy definition has {len(rel_def_ids)} RelationshipType entries "
                    f"but model declares {model_rel_count}. "
                    f"{model_rel_count - len(rel_def_ids)} relationship type(s) missing."
                ),
            )
        )

    # Contextualization check: every declared RelationshipType must have ≥1
    for rt_id in rel_def_ids:
        if rt_id not in rel_ctx_ids:
            violations.append(
                IdentityViolation(
                    gate_id="OKV-001",
                    severity="error",
                    message=(
                        f"RelationshipType '{rt_id}' has zero Contextualizations in the "
                        f"post-deploy definition. Cannot publish a disconnected relationship "
                        f"(ONTOLOGY_RELATIONSHIP_KEY_MISMATCH)."
                    ),
                )
            )

    return violations
