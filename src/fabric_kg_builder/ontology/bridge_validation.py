"""Bridge validation: BRG-001..010 gates for the graph-to-search bridge.

Validates that the ontology model.yaml correctly declares the properties and
data-binding columns required for two-phase retrieval traversal:

    domain entity  --[evidenced_by]-->  DocumentChunk  --[indexed_as]-->  SearchIndexRecord
    domain entity  --[shown_in]-->      Figure / ImageAsset

Gates are defined in SPEC-003 §12.9.  Call ``validate_bridge(model)`` and
inspect the returned :class:`BridgeViolation` list.  Violations with
``severity == "error"`` must block the build; ``"warning"`` violations are
logged only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class BridgeViolation:
    """A single BRG validation gate failure.

    Attributes:
        gate_id:  Gate identifier (e.g. ``"BRG-001"``).
        severity: ``"error"`` (build must fail) or ``"warning"`` (log only).
        message:  Human-readable description of the violation.
    """

    gate_id: str
    severity: str  # "error" | "warning"
    message: str


# ---------------------------------------------------------------------------
# Canonical bridge constants
# ---------------------------------------------------------------------------

# Support-domain properties required on every entity type in that module
_SUPPORT_DOMAIN_REQUIRED_PROPS = ("entity_id", "canonical_key", "search_aliases")

# Properties required on DocumentChunk for bridge traversal (BRG-001)
_DOCUMENT_CHUNK_REQUIRED_PROPS = ("entity_id", "chunk_id", "related_entity_ids", "entity_search_keys")

# Properties required on SearchIndexRecord (BRG-002)
_SEARCH_INDEX_RECORD_REQUIRED_PROPS = ("search_record_id",)

# Visual entity types that must carry blob_url (BRG-004)
_BLOB_URL_REQUIRED_ENTITY_TYPES = ("ImageAsset", "Figure")

# Bridge relationship names that must exist with inversePolicy set (BRG-005)
_BRIDGE_REL_NAMES = ("evidenced_by", "shown_in", "indexed_as")

# Support-domain module name
_SUPPORT_DOMAIN = "support-domain"

# Canonical tables for the bridge bindings (for column-level checks)
_CHUNKS_TABLE = "chunks"
_VISUAL_ASSETS_TABLE = "visual_assets"
_DOCUMENT_ELEMENTS_TABLE = "document_elements"

# Columns that bridge target entity types must bind (table → required columns)
_REQUIRED_BINDINGS: dict[str, set[str]] = {
    # DocumentChunk binds to chunks table — must expose these columns
    "DocumentChunk": {"related_entity_ids", "entity_search_keys", "chunk_id"},
    # SearchIndexRecord must bind chunk_id to search_record_id
    "SearchIndexRecord": {"chunk_id"},
    # Figure must bind blob_url column
    "Figure": {"blob_url"},
    # ImageAsset must bind blob_url column
    "ImageAsset": {"blob_url"},
}


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------


def _entity_types(model: dict[str, Any]) -> list[dict[str, Any]]:
    return model.get("entityTypes", [])


def _rel_types(model: dict[str, Any]) -> list[dict[str, Any]]:
    return model.get("relationshipTypes", [])


def _entity_by_name(model: dict[str, Any], name: str) -> dict[str, Any] | None:
    for et in _entity_types(model):
        if et.get("name") == name:
            return et
    return None


def _rel_by_name(model: dict[str, Any], name: str) -> dict[str, Any] | None:
    for rt in _rel_types(model):
        if rt.get("name") == name:
            return rt
    return None


def _prop_names(entity_type: dict[str, Any]) -> set[str]:
    return {p["name"] for p in entity_type.get("properties", [])}


def _bound_columns(entity_type: dict[str, Any]) -> set[str]:
    """Return the set of canonical *column* names referenced in the dataBinding."""
    db = entity_type.get("dataBinding", {})
    cols: set[str] = set()
    # Primary binding columns
    for key in ("entityIdColumn", "displayNameColumn", "typeFilterColumn",
                "sourceEntityIdColumn", "targetEntityIdColumn", "relationshipIdColumn"):
        val = db.get(key)
        if val:
            cols.add(val)
    # Additional column mappings — we want the *column* (rhs), not the property name (lhs)
    for mapping in db.get("additionalColumns", []):
        col = mapping.get("column")
        if col:
            cols.add(col)
    return cols


def _support_domain_entity_names(model: dict[str, Any]) -> set[str]:
    return {
        et["name"]
        for et in _entity_types(model)
        if et.get("module") == _SUPPORT_DOMAIN
    }


def _bridge_source_types(model: dict[str, Any]) -> set[str]:
    """Entity type names that are the source of evidenced_by or shown_in."""
    sources: set[str] = set()
    for rt in _rel_types(model):
        if rt.get("name") in ("evidenced_by", "shown_in"):
            src = rt.get("sourceType")
            if src:
                sources.add(src)
    return sources


# ---------------------------------------------------------------------------
# Individual gate checks
# ---------------------------------------------------------------------------


def _check_brg001(model: dict[str, Any]) -> list[BridgeViolation]:
    """BRG-001: DocumentChunk declares the four required bridge properties."""
    violations: list[BridgeViolation] = []
    et = _entity_by_name(model, "DocumentChunk")
    if et is None:
        violations.append(BridgeViolation(
            "BRG-001", "error",
            "Entity type 'DocumentChunk' not found in model.yaml — required for bridge traversal",
        ))
        return violations

    declared = _prop_names(et)
    for prop in _DOCUMENT_CHUNK_REQUIRED_PROPS:
        if prop not in declared:
            violations.append(BridgeViolation(
                "BRG-001", "error",
                f"DocumentChunk is missing required bridge property '{prop}' "
                f"(needed for chunks.{prop} canonical column binding)",
            ))

    # Also verify the properties are bound to actual chunk columns
    bound = _bound_columns(et)
    for col in _REQUIRED_BINDINGS["DocumentChunk"]:
        if col not in bound:
            violations.append(BridgeViolation(
                "BRG-001", "error",
                f"DocumentChunk dataBinding does not bind canonical column '{col}' "
                f"(required by bridge: chunks.{col})",
            ))
    return violations


def _check_brg002(model: dict[str, Any]) -> list[BridgeViolation]:
    """BRG-002: SearchIndexRecord declares search_record_id."""
    violations: list[BridgeViolation] = []
    et = _entity_by_name(model, "SearchIndexRecord")
    if et is None:
        violations.append(BridgeViolation(
            "BRG-002", "error",
            "Entity type 'SearchIndexRecord' not found — required for indexed_as bridge hop",
        ))
        return violations

    declared = _prop_names(et)
    for prop in _SEARCH_INDEX_RECORD_REQUIRED_PROPS:
        if prop not in declared:
            violations.append(BridgeViolation(
                "BRG-002", "error",
                f"SearchIndexRecord is missing required bridge property '{prop}'",
            ))

    # Verify binding: search_record_id must map to chunks.chunk_id
    bound = _bound_columns(et)
    if "chunk_id" not in bound:
        violations.append(BridgeViolation(
            "BRG-002", "error",
            "SearchIndexRecord dataBinding does not bind 'chunk_id' column "
            "(search_record_id must map to chunks.chunk_id at index-build time)",
        ))
    return violations


def _check_brg003(model: dict[str, Any]) -> list[BridgeViolation]:
    """BRG-003: Every support-domain entity type declares entity_id, canonical_key, search_aliases."""
    violations: list[BridgeViolation] = []
    for et in _entity_types(model):
        if et.get("module") != _SUPPORT_DOMAIN:
            continue
        name = et["name"]
        declared = _prop_names(et)
        for prop in _SUPPORT_DOMAIN_REQUIRED_PROPS:
            if prop not in declared:
                violations.append(BridgeViolation(
                    "BRG-003", "error",
                    f"support-domain entity type '{name}' is missing required property '{prop}' "
                    f"(needed for AI Search entity filter / canonical key lookup)",
                ))
    return violations


def _check_brg004(model: dict[str, Any]) -> list[BridgeViolation]:
    """BRG-004: ImageAsset and Figure declare blob_url property (format uri)."""
    violations: list[BridgeViolation] = []
    for type_name in _BLOB_URL_REQUIRED_ENTITY_TYPES:
        et = _entity_by_name(model, type_name)
        if et is None:
            violations.append(BridgeViolation(
                "BRG-004", "error",
                f"Entity type '{type_name}' not found — must exist with blob_url for visual grounding",
            ))
            continue
        # Must have blob_url property of type blob_url
        blob_prop = next(
            (p for p in et.get("properties", []) if p.get("name") == "blob_url"),
            None,
        )
        if blob_prop is None:
            violations.append(BridgeViolation(
                "BRG-004", "error",
                f"'{type_name}' is missing 'blob_url' property (required for visual grounding — SPEC-003 §7)",
            ))
        elif blob_prop.get("type") != "blob_url":
            violations.append(BridgeViolation(
                "BRG-004", "error",
                f"'{type_name}.blob_url' must have type 'blob_url' (format uri), "
                f"got '{blob_prop.get('type')}'",
            ))
        # Also check data binding exposes the blob_url column
        bound = _bound_columns(et)
        if "blob_url" not in bound:
            violations.append(BridgeViolation(
                "BRG-004", "error",
                f"'{type_name}' dataBinding does not bind 'blob_url' column "
                f"(visual grounding URL must be fetchable in Phase 1 traversal)",
            ))
    return violations


def _check_brg005(model: dict[str, Any]) -> list[BridgeViolation]:
    """BRG-005: evidenced_by, shown_in, indexed_as exist with inversePolicy set."""
    violations: list[BridgeViolation] = []
    for rel_name in _BRIDGE_REL_NAMES:
        rt = _rel_by_name(model, rel_name)
        if rt is None:
            violations.append(BridgeViolation(
                "BRG-005", "error",
                f"Bridge relationship type '{rel_name}' not found in model.yaml — "
                f"required for graph-to-search traversal",
            ))
        elif "inversePolicy" not in rt:
            violations.append(BridgeViolation(
                "BRG-005", "error",
                f"Bridge relationship '{rel_name}' has no 'inversePolicy' field — "
                f"all bridge relationships must declare inversePolicy (may be 'none')",
            ))
    return violations


def _check_brg006(model: dict[str, Any]) -> list[BridgeViolation]:
    """BRG-006: indexed_as resolves — SearchIndexRecord has search_record_id bound."""
    violations: list[BridgeViolation] = []
    rt = _rel_by_name(model, "indexed_as")
    if rt is None:
        # BRG-005 already reports missing; skip here
        return violations

    target_name = rt.get("targetType", "")
    et = _entity_by_name(model, target_name)
    if et is None:
        violations.append(BridgeViolation(
            "BRG-006", "error",
            f"indexed_as.targetType='{target_name}' not found as an entity type — "
            f"bridge cannot resolve to a SearchIndexRecord node",
        ))
        return violations

    if "search_record_id" not in _prop_names(et):
        violations.append(BridgeViolation(
            "BRG-006", "error",
            f"indexed_as target '{target_name}' is missing 'search_record_id' property — "
            f"bridge traversal cannot yield the AI Search document key",
        ))
    if "chunk_id" not in _bound_columns(et):
        violations.append(BridgeViolation(
            "BRG-006", "error",
            f"indexed_as target '{target_name}' dataBinding does not bind 'chunk_id' column — "
            f"search_record_id must resolve to chunks.chunk_id",
        ))
    return violations


def _check_brg007(model: dict[str, Any]) -> list[BridgeViolation]:
    """BRG-007: evidenced_by resolves — DocumentChunk has chunk_id bound."""
    violations: list[BridgeViolation] = []
    rt = _rel_by_name(model, "evidenced_by")
    if rt is None:
        return violations

    target_name = rt.get("targetType", "")
    et = _entity_by_name(model, target_name)
    if et is None:
        violations.append(BridgeViolation(
            "BRG-007", "error",
            f"evidenced_by.targetType='{target_name}' not found as an entity type — "
            f"bridge cannot resolve to a DocumentChunk node",
        ))
        return violations

    if "chunk_id" not in _prop_names(et):
        violations.append(BridgeViolation(
            "BRG-007", "error",
            f"evidenced_by target '{target_name}' is missing 'chunk_id' property — "
            f"bridge traversal cannot yield chunk_id for AI Search lookup",
        ))
    if "chunk_id" not in _bound_columns(et):
        violations.append(BridgeViolation(
            "BRG-007", "error",
            f"evidenced_by target '{target_name}' dataBinding does not bind 'chunk_id' column "
            f"(chunks.chunk_id is the primary key for AI Search lookup)",
        ))
    return violations


def _check_brg008(model: dict[str, Any]) -> list[BridgeViolation]:
    """BRG-008: shown_in resolves — target Figure/ImageAsset has blob_url bound."""
    violations: list[BridgeViolation] = []
    rt = _rel_by_name(model, "shown_in")
    if rt is None:
        return violations

    target_name = rt.get("targetType", "")
    # shown_in may target Figure OR ImageAsset — check whichever is declared
    candidates = [target_name] if target_name else []

    for name in candidates:
        et = _entity_by_name(model, name)
        if et is None:
            violations.append(BridgeViolation(
                "BRG-008", "error",
                f"shown_in.targetType='{name}' not found as an entity type — "
                f"bridge cannot resolve to a visual node with blob_url",
            ))
            continue
        if "blob_url" not in _prop_names(et):
            violations.append(BridgeViolation(
                "BRG-008", "error",
                f"shown_in target '{name}' is missing 'blob_url' property — "
                f"visual grounding requires blob_url to pass to vision model",
            ))
        if "blob_url" not in _bound_columns(et):
            violations.append(BridgeViolation(
                "BRG-008", "error",
                f"shown_in target '{name}' dataBinding does not bind 'blob_url' column — "
                f"blob_url must be fetchable in Phase 1 graph traversal",
            ))
    return violations


def _check_brg009(model: dict[str, Any]) -> list[BridgeViolation]:
    """BRG-009 (Warning): support-domain entities with no outbound bridge edge.

    This is diagnostic only — entities with no evidenced_by or shown_in edge
    cannot participate in Phase 1 traversal and will fall back to pure AI Search.
    """
    violations: list[BridgeViolation] = []
    support_names = _support_domain_entity_names(model)
    bridged_sources = _bridge_source_types(model)
    unlinked = sorted(support_names - bridged_sources)
    for name in unlinked:
        violations.append(BridgeViolation(
            "BRG-009", "warning",
            f"support-domain entity '{name}' has no outbound 'evidenced_by' or 'shown_in' "
            f"relationship — it cannot participate in Phase 1 bridge traversal "
            f"(will fall back to pure AI Search; add an evidenced_by edge to enable graph grounding)",
        ))
    return violations


def _check_brg010(model: dict[str, Any]) -> list[BridgeViolation]:
    """BRG-010: entity_id property on support-domain nodes has a non-empty column binding."""
    violations: list[BridgeViolation] = []
    for et in _entity_types(model):
        if et.get("module") != _SUPPORT_DOMAIN:
            continue
        name = et["name"]
        props = {p["name"]: p for p in et.get("properties", [])}
        if "entity_id" not in props:
            # BRG-003 already catches this; skip
            continue
        # The entity_id column binding must be non-empty
        db = et.get("dataBinding", {})
        entity_id_col = db.get("entityIdColumn", "")
        if not entity_id_col:
            violations.append(BridgeViolation(
                "BRG-010", "error",
                f"support-domain entity '{name}' has empty entityIdColumn in dataBinding — "
                f"entity_id cannot be populated (risk of duplicate/null IDs in graph)",
            ))
    return violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_bridge(model: dict[str, Any]) -> list[BridgeViolation]:
    """Run all BRG-001..010 validation gates against *model*.

    Args:
        model: The parsed ontology model dict (the value of the ``ontology:``
               key in ``model.yaml``, i.e. already unwrapped).

    Returns:
        A (possibly empty) list of :class:`BridgeViolation` objects.
        Violations with ``severity == "error"`` must block the build.
        Violations with ``severity == "warning"`` should be logged only.
    """
    violations: list[BridgeViolation] = []
    violations.extend(_check_brg001(model))
    violations.extend(_check_brg002(model))
    violations.extend(_check_brg003(model))
    violations.extend(_check_brg004(model))
    violations.extend(_check_brg005(model))
    violations.extend(_check_brg006(model))
    violations.extend(_check_brg007(model))
    violations.extend(_check_brg008(model))
    violations.extend(_check_brg009(model))
    violations.extend(_check_brg010(model))
    return violations
