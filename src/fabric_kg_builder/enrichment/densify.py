"""Deterministic, explicitly configured graph densification.

Extraction can leave related entities disconnected when they occur in separate
text units. This module can add document-scoped hub, associative, sequence, and
diagnostic-path edges, but it never guesses a domain taxonomy. Every entity type
and relationship verb must come from an explicit :class:`DensifyConfig`.

The default configuration is intentionally empty. This keeps production behavior
domain-neutral and prevents a sample taxonomy from being injected into unrelated
domains. Added edges are deterministic, idempotent, marked as inferred, and
inherit lineage from their source entity.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from fabric_kg_builder.model.ids import content_hash, make_relationship_id

logger = logging.getLogger(__name__)

_COMMON_LINEAGE_FIELDS = (
    "asset_id",
    "asset_version_id",
    "run_id",
    "source_locator_json",
    "schema_version",
    "domain_hash",
)


@dataclass(frozen=True)
class DensifyConfig:
    """Type and relationship mappings used by the densification passes."""

    hub_source_types: tuple[str, ...] = ()
    hub_qualification: str = "specific"
    hub_target_relationships: dict[str, str] = field(default_factory=dict)
    cause_types: tuple[str, ...] = ()
    symptom_types: tuple[str, ...] = ()
    resolution_types: tuple[str, ...] = ()
    cause_symptom_relationship: str = ""
    symptom_resolution_relationship: str = ""
    cause_resolution_relationship: str = ""
    procedure_types: tuple[str, ...] = ()
    step_types: tuple[str, ...] = ()
    procedure_step_relationship: str = ""
    rca_symptom_types: tuple[str, ...] = ()
    rca_procedure_types: tuple[str, ...] = ()
    rca_diagnosed_by_relationship: str = ""
    rca_remediated_by_relationship: str = ""
    umbrella_patterns: tuple[str, ...] = ()

    @property
    def has_rules(self) -> bool:
        """Return whether at least one densification pass is configured."""
        return bool(
            self.hub_source_types
            or self.cause_types
            or self.procedure_types
            or self.rca_symptom_types
            or self.umbrella_patterns
        )

    @property
    def entity_types(self) -> frozenset[str]:
        """Return all configured entity type names."""
        return frozenset(
            (
                *self.hub_source_types,
                *self.hub_target_relationships.keys(),
                *self.cause_types,
                *self.symptom_types,
                *self.resolution_types,
                *self.procedure_types,
                *self.step_types,
                *self.rca_symptom_types,
                *self.rca_procedure_types,
            )
        )

    @property
    def relationship_types(self) -> frozenset[str]:
        """Return all configured relationship type names."""
        return frozenset(
            value
            for value in (
                *self.hub_target_relationships.values(),
                self.cause_symptom_relationship,
                self.symptom_resolution_relationship,
                self.cause_resolution_relationship,
                self.procedure_step_relationship,
                self.rca_diagnosed_by_relationship,
                self.rca_remediated_by_relationship,
            )
            if value
        )

    def validate_complete(self) -> None:
        """Reject partially configured rules that could infer ambiguous edges."""
        if bool(self.hub_source_types) != bool(self.hub_target_relationships):
            raise ValueError(
                "densify config 'hub' requires both source_types and target_relationships"
            )
        scr_values = (
            self.cause_types,
            self.symptom_types,
            self.resolution_types,
            self.cause_symptom_relationship,
            self.symptom_resolution_relationship,
            self.cause_resolution_relationship,
        )
        if any(scr_values) and not all(scr_values):
            raise ValueError(
                "densify config 'scr' requires all three type lists and relationship verbs"
            )
        procedure_values = (
            self.procedure_types,
            self.step_types,
            self.procedure_step_relationship,
        )
        if any(procedure_values) and not all(procedure_values):
            raise ValueError(
                "densify config 'procedure_steps' requires procedure_types, step_types, and relationship"
            )
        rca_values = (
            self.rca_symptom_types,
            self.rca_procedure_types,
            self.rca_diagnosed_by_relationship,
            self.rca_remediated_by_relationship,
        )
        if any(rca_values) and not all(rca_values):
            raise ValueError(
                "densify config 'rca' requires both type lists and relationship verbs"
            )
        if self.umbrella_patterns and not all(procedure_values):
            raise ValueError(
                "densify config 'umbrella' requires a complete procedure_steps rule"
            )

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "DensifyConfig":
        """Create a config from a YAML mapping, retaining omitted defaults."""
        def section(name: str) -> dict[str, Any]:
            value = raw.get(name, {})
            if not isinstance(value, dict):
                raise ValueError(f"densify config '{name}' must be a mapping")
            return value

        def types(value: Any, default: tuple[str, ...], name: str) -> tuple[str, ...]:
            if value is None:
                return default
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise ValueError(f"densify config '{name}' must be a list of strings")
            return tuple(item.strip() for item in value)

        defaults = cls()
        hub = section("hub")
        scr = section("scr")
        procedure_steps = section("procedure_steps")
        rca = section("rca")
        umbrella = section("umbrella")
        qualification = hub.get("qualification", defaults.hub_qualification)
        if qualification not in {"specific", "any"}:
            raise ValueError("densify config 'hub.qualification' must be 'specific' or 'any'")
        target_relationships = hub.get("target_relationships", defaults.hub_target_relationships)
        if not isinstance(target_relationships, dict) or not all(
            isinstance(entity_type, str)
            and entity_type.strip()
            and isinstance(relationship, str)
            and relationship.strip()
            for entity_type, relationship in target_relationships.items()
        ):
            raise ValueError("densify config 'hub.target_relationships' must be a string mapping")
        patterns = umbrella.get("patterns", defaults.umbrella_patterns)
        if not isinstance(patterns, (list, tuple)) or not all(
            isinstance(pattern, str) and pattern for pattern in patterns
        ):
            raise ValueError("densify config 'umbrella.patterns' must be a list of regular expressions")
        try:
            for pattern in patterns:
                re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid densify umbrella pattern: {exc}") from exc

        def verb(mapping: dict[str, Any], name: str, default: str) -> str:
            value = mapping.get(name, default)
            if not isinstance(value, str):
                raise ValueError(f"densify config '{name}' must be a string")
            return value.strip()

        config = cls(
            hub_source_types=types(hub.get("source_types"), defaults.hub_source_types, "hub.source_types"),
            hub_qualification=qualification,
            hub_target_relationships={
                entity_type.strip(): relationship.strip()
                for entity_type, relationship in target_relationships.items()
            },
            cause_types=types(scr.get("cause_types"), defaults.cause_types, "scr.cause_types"),
            symptom_types=types(scr.get("symptom_types"), defaults.symptom_types, "scr.symptom_types"),
            resolution_types=types(scr.get("resolution_types"), defaults.resolution_types, "scr.resolution_types"),
            cause_symptom_relationship=verb(scr, "cause_symptom_relationship", defaults.cause_symptom_relationship),
            symptom_resolution_relationship=verb(scr, "symptom_resolution_relationship", defaults.symptom_resolution_relationship),
            cause_resolution_relationship=verb(scr, "cause_resolution_relationship", defaults.cause_resolution_relationship),
            procedure_types=types(procedure_steps.get("procedure_types"), defaults.procedure_types, "procedure_steps.procedure_types"),
            step_types=types(procedure_steps.get("step_types"), defaults.step_types, "procedure_steps.step_types"),
            procedure_step_relationship=verb(procedure_steps, "relationship", defaults.procedure_step_relationship),
            rca_symptom_types=types(rca.get("symptom_types"), defaults.rca_symptom_types, "rca.symptom_types"),
            rca_procedure_types=types(rca.get("procedure_types"), defaults.rca_procedure_types, "rca.procedure_types"),
            rca_diagnosed_by_relationship=verb(rca, "diagnosed_by_relationship", defaults.rca_diagnosed_by_relationship),
            rca_remediated_by_relationship=verb(rca, "remediated_by_relationship", defaults.rca_remediated_by_relationship),
            umbrella_patterns=tuple(patterns),
        )
        config.validate_complete()
        return config


DEFAULT_DENSIFY_CONFIG = DensifyConfig()


def load_densify_config(path: str | Path) -> DensifyConfig:
    """Load a YAML densification configuration file."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("densify config must be a YAML mapping")
    return DensifyConfig.from_mapping(raw)

_GENERIC_NAMES = {
    "concept",
    "concepts",
    "entity",
    "entities",
    "item",
    "items",
    "model",
    "models",
    "object",
    "objects",
    "record",
    "records",
    "thing",
    "things",
    "unknown",
    "unspecified",
}


def is_specific_hub_name(display_name: str | None) -> bool:
    """Return whether a hub name is concrete rather than a placeholder."""
    if not display_name:
        return False
    name = re.sub(r"\s+", " ", display_name).strip().lower()
    if name in _GENERIC_NAMES:
        return False
    if name.startswith(("this ", "the current ", "an unspecified ", "unknown ")):
        return False
    return bool(re.search(r"[a-z0-9]", name)) and len(name) >= 3


def _is_qualified_hub(display_name: str | None, qualification: str) -> bool:
    if qualification == "specific":
        return is_specific_hub_name(display_name)
    if qualification == "any":
        return bool(display_name and display_name.strip().lower() not in _GENERIC_NAMES)
    raise ValueError(f"unsupported hub qualification: {qualification}")


def _new_inferred_relationship(
    rel_type: str,
    source_entity: dict[str, Any],
    target_entity: dict[str, Any],
    *,
    origin: str,
    confidence: float,
) -> dict[str, Any]:
    source_entity_id = source_entity["entity_id"]
    target_entity_id = target_entity["entity_id"]
    row = {
        "relationship_id": make_relationship_id(rel_type, source_entity_id, target_entity_id),
        "relationship_type": rel_type,
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "evidence_id": None,
        "properties_json": json.dumps(
            {"origin": origin, "provenance_origin": "inferred"},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "confidence": confidence,
        "is_placeholder": False,
        "content_hash": content_hash(f"{rel_type}:{source_entity_id}:{target_entity_id}"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parent_record_id": source_entity_id,
    }
    for field_name in _COMMON_LINEAGE_FIELDS:
        if field_name in source_entity:
            row[field_name] = source_entity[field_name]
    return row


def densify_document(
    doc: dict[str, Any], max_hubs: int = 5, config: DensifyConfig | None = None,
) -> tuple[dict[str, Any], int]:
    """Add explicitly configured hub-to-entity edges to one document.

    Parameters
    ----------
    doc:
        A parsed enriched canonical document (one source file) with
        ``entities`` and ``relationships`` lists.
    max_hubs:
        Cap on the number of qualified hubs per document.

    Returns
    -------
    (doc, added):
        The same ``doc`` dict (mutated in place) and the count of new edges.
    """
    entities = doc.get("entities") or []
    relationships = doc.get("relationships") or []
    config = config or DEFAULT_DENSIFY_CONFIG

    hubs = [
        e for e in entities
        if e.get("entity_type") in config.hub_source_types
        and _is_qualified_hub(e.get("display_name"), config.hub_qualification)
    ]
    hubs.sort(key=lambda e: len(e.get("display_name") or ""), reverse=True)
    hubs = hubs[:max_hubs]
    if not hubs:
        return doc, 0

    # Existing (source, target) pairs — never duplicate.
    existing_pairs: set[tuple[str, str]] = {
        (r.get("source_entity_id"), r.get("target_entity_id")) for r in relationships
    }

    # Targets: linkable entities of the hub-eligible types in this document.
    new_rels: list[dict[str, Any]] = []
    for ent in entities:
        rel_type = config.hub_target_relationships.get(ent.get("entity_type"))
        if rel_type is None:
            continue
        tgt = ent["entity_id"]
        for hub in hubs:
            hub_id = hub["entity_id"]
            if hub_id == tgt:
                continue
            if (hub_id, tgt) in existing_pairs:
                continue
            existing_pairs.add((hub_id, tgt))
            new_rels.append(
                _new_inferred_relationship(
                    rel_type,
                    hub,
                    ent,
                    origin="densify:source-hub",
                    confidence=0.5,
                )
            )

    relationships.extend(new_rels)
    doc["relationships"] = relationships
    return doc, len(new_rels)


# ---------------------------------------------------------------------------
# Explicit three-stage associative linking
# ---------------------------------------------------------------------------

# Tokens too generic to be a useful linking signal. Domain-specific ubiquitous
# terms are removed dynamically by the document-frequency gate.
_SCR_STOPWORDS = set(
    "the a an and or of to in on for with without your you this that these those is "
    "are be by from at as it its their them they we our using use used into not no "
    "non should must may can will if when before after during while also other more "
    "most some any all per via due such only than then them "
    "this that have has had been being which what when where how who whom each".split()
)

# Confidence assigned to inferred associative edges.
_SCR_CONFIDENCE = 0.45


def _salient_tokens(display_name: str | None) -> set[str]:
    """Significant lowercase tokens from *display_name* (len ≥ 4, not stopwords)."""
    return {
        w
        for w in re.findall(r"[a-z0-9]+", (display_name or "").lower())
        if len(w) >= 4 and w not in _SCR_STOPWORDS
    }


def _inferred_scr_relationship(
    rel_type: str,
    source_entity: dict[str, Any],
    target_entity: dict[str, Any],
    origin: str,
    confidence: float = _SCR_CONFIDENCE,
) -> dict[str, Any]:
    return _new_inferred_relationship(
        rel_type,
        source_entity,
        target_entity,
        origin=origin,
        confidence=confidence,
    )


def link_symptom_cause_resolution(
    doc: dict[str, Any],
    top_k: int = 3,
    ubiquity_ratio: float = 0.4,
    min_shared: int = 1,
    config: DensifyConfig | None = None,
) -> tuple[dict[str, Any], int]:
    """Link an explicitly configured three-stage pattern within one document.

    Uses document-scoped keyword overlap gated by token specificity. Terms that
    occur across too many configured entities are ignored. For each middle-stage
    entity, the top ``top_k`` first- and third-stage entities are linked, plus a
    transitive first-to-third edge when both associations are supported.

    Inferred edges carry confidence 0.45 and an ``origin`` tag. Deterministic,
    idempotent (existing pairs never duplicated), non-destructive.

    Returns ``(doc, added)``.
    """
    entities = doc.get("entities") or []
    relationships = doc.get("relationships") or []
    config = config or DEFAULT_DENSIFY_CONFIG

    causes = [e for e in entities if e.get("entity_type") in config.cause_types]
    symptoms = [e for e in entities if e.get("entity_type") in config.symptom_types]
    resolutions = [e for e in entities if e.get("entity_type") in config.resolution_types]
    if not symptoms or (not causes and not resolutions):
        return doc, 0

    scr = causes + symptoms + resolutions
    tokens: dict[str, set[str]] = {e["entity_id"]: _salient_tokens(e.get("display_name")) for e in scr}

    # Document frequency of each token across S/C/R; drop ubiquitous tokens.
    df: dict[str, int] = {}
    for e in scr:
        for w in tokens[e["entity_id"]]:
            df[w] = df.get(w, 0) + 1
    n = len(scr)
    max_df = max(2, int(ubiquity_ratio * n))
    discriminating = {w for w, c in df.items() if 2 <= c <= max_df}

    def shared_count(a: str, b: str) -> int:
        return len(tokens[a] & tokens[b] & discriminating)

    existing_pairs: set[tuple[str, str]] = {
        (r.get("source_entity_id"), r.get("target_entity_id")) for r in relationships
    }
    new_rels: list[dict[str, Any]] = []

    entity_by_id = {entity["entity_id"]: entity for entity in scr}

    def add(rel_type: str, src: str, tgt: str, origin: str) -> None:
        if src == tgt or (src, tgt) in existing_pairs:
            return
        existing_pairs.add((src, tgt))
        new_rels.append(
            _inferred_scr_relationship(
                rel_type,
                entity_by_id[src],
                entity_by_id[tgt],
                origin,
            )
        )

    # Middle stage → ranked first/third stages, plus the transitive shortcut.
    for s in symptoms:
        sid = s["entity_id"]
        ranked_causes = sorted(
            ((shared_count(sid, c["entity_id"]), c["entity_id"]) for c in causes),
            key=lambda x: -x[0],
        )
        linked_causes = [cid for ov, cid in ranked_causes[:top_k] if ov >= min_shared]

        ranked_res = sorted(
            ((shared_count(sid, r["entity_id"]), r["entity_id"]) for r in resolutions),
            key=lambda x: -x[0],
        )
        linked_res = [rid for ov, rid in ranked_res[:top_k] if ov >= min_shared]

        for cid in linked_causes:
            add(config.cause_symptom_relationship, cid, sid, "densify:scr-keyword")
        for rid in linked_res:
            add(config.symptom_resolution_relationship, sid, rid, "densify:scr-keyword")
        # High precision: both endpoints share the same middle-stage entity.
        for cid in linked_causes:
            for rid in linked_res:
                add(config.cause_resolution_relationship, cid, rid, "densify:scr-transitive")

    relationships.extend(new_rels)
    doc["relationships"] = relationships
    return doc, len(new_rels)


# ---------------------------------------------------------------------------
# Configured parent → child linking by document reading order
# ---------------------------------------------------------------------------
#
# Map configured parent and child entities to their positions in document
# elements, then assign each child to the nearest preceding parent.

_PROC_STEP_CONFIDENCE = 0.5
# A Step links to the current procedure only if it appears within this many
# elements after it (guards against a trailing step bleeding into a far-away
# procedure when a section has no steps of its own).
_PROC_STEP_MAX_GAP = 40


def _build_element_index(document_elements: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    """Return [(page, sort_order, lowercased_content), ...] sorted by reading order."""
    rows: list[tuple[int, int, str]] = []
    for el in document_elements or []:
        page = el.get("page_number")
        so = el.get("sort_order")
        content = (el.get("content") or "").lower()
        if content:
            rows.append((page if page is not None else 0, so if so is not None else 0, content))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def _entity_position(display_name: str, index: list[tuple[int, int, str]]) -> tuple[int, int] | None:
    """Best (page, sort_order) for *display_name* via first text containment."""
    key = (display_name or "").strip().lower()[:28]
    if not key:
        return None
    for page, so, content in index:
        if key in content:
            return (page, so)
    return None


def link_procedure_steps(
    doc: dict[str, Any],
    max_steps_per_procedure: int = 60,
    config: DensifyConfig | None = None,
) -> tuple[dict[str, Any], int]:
    """Link configured child entities to their nearest preceding parent.

    Maps configured types to positions using ``document_elements`` text, walks
    the merged sequence in reading order, and attaches each child to the most
    recent parent (within
    :data:`_PROC_STEP_MAX_GAP` elements). Deterministic, idempotent,
    non-destructive; inferred edges carry confidence 0.5 and an origin tag.

    Returns ``(doc, added)``.
    """
    entities = doc.get("entities") or []
    relationships = doc.get("relationships") or []
    index = _build_element_index(doc.get("document_elements") or [])
    config = config or DEFAULT_DENSIFY_CONFIG
    if not index:
        return doc, 0

    procedures = [e for e in entities if e.get("entity_type") in config.procedure_types]
    steps = [e for e in entities if e.get("entity_type") in config.step_types]
    if not procedures or not steps:
        return doc, 0

    # Position each procedure / step; build an ordinal index for gap checks.
    ordinal = {(p, s): i for i, (p, s, _c) in enumerate(index)}

    def ordinal_of(pos: tuple[int, int] | None) -> int | None:
        return ordinal.get(pos) if pos is not None else None

    placed: list[tuple[int, str, str]] = []  # (ordinal, kind, entity_id)
    for p in procedures:
        o = ordinal_of(_entity_position(p["display_name"], index))
        if o is not None:
            placed.append((o, "P", p["entity_id"]))
    for s in steps:
        o = ordinal_of(_entity_position(s["display_name"], index))
        if o is not None:
            placed.append((o, "S", s["entity_id"]))
    if not placed:
        return doc, 0
    placed.sort(key=lambda x: x[0])

    existing_pairs: set[tuple[str, str]] = {
        (r.get("source_entity_id"), r.get("target_entity_id")) for r in relationships
    }
    new_rels: list[dict[str, Any]] = []
    entity_by_id = {entity["entity_id"]: entity for entity in entities}
    cur_proc: str | None = None
    cur_proc_ord: int | None = None
    per_proc: dict[str, int] = {}

    for o, kind, eid in placed:
        if kind == "P":
            cur_proc = eid
            cur_proc_ord = o
            continue
        # Step: attach to current procedure if within the allowed gap.
        if cur_proc is None or cur_proc_ord is None:
            continue
        if o - cur_proc_ord > _PROC_STEP_MAX_GAP:
            continue
        if per_proc.get(cur_proc, 0) >= max_steps_per_procedure:
            continue
        if cur_proc == eid or (cur_proc, eid) in existing_pairs:
            continue
        existing_pairs.add((cur_proc, eid))
        per_proc[cur_proc] = per_proc.get(cur_proc, 0) + 1
        new_rels.append(
            _new_inferred_relationship(
                config.procedure_step_relationship,
                entity_by_id[cur_proc],
                entity_by_id[eid],
                origin="densify:sequence-reading-order",
                confidence=_PROC_STEP_CONFIDENCE,
            )
        )

    relationships.extend(new_rels)
    doc["relationships"] = relationships
    return doc, len(new_rels)


# ---------------------------------------------------------------------------
# Optional diagnostic-path linking
# ---------------------------------------------------------------------------
#
# Process names are classified as diagnostic versus remediating by configurable
# entity types and conservative name signals, then linked by discriminating
# keyword overlap.

_RCA_CONFIDENCE = 0.4

# Procedure-name keywords that mark a procedure as a diagnostic test/check
# (the reviewer's "DiagnosticTest" -- real entities already in the corpus).
_DIAGNOSTIC_KEYWORDS = (
    "sdt", "diagnos", "check", "test", "inspect", "validat", "verify",
    "status", "troubleshoot", "detect", "scan",
)


def is_diagnostic_procedure(display_name):
    """True if *display_name* looks like a diagnostic test/check procedure."""
    if not display_name:
        return False
    name = display_name.lower()
    return any(k in name for k in _DIAGNOSTIC_KEYWORDS)


def link_rca_paths(doc, top_k=3, ubiquity_ratio=0.4, min_shared=1, config: DensifyConfig | None = None):
    """Link configured issue-like entities to diagnostic/remediation processes.

    Scores configured entity/process pairs by shared discriminating tokens. For
    the top ``top_k`` processes, uses the configured diagnostic relationship
    when the process name matches :func:`is_diagnostic_procedure`; otherwise it
    uses the configured remediation relationship.
    """
    entities = doc.get("entities") or []
    relationships = doc.get("relationships") or []
    config = config or DEFAULT_DENSIFY_CONFIG

    symptoms = [e for e in entities if e.get("entity_type") in config.rca_symptom_types]
    procedures = [e for e in entities if e.get("entity_type") in config.rca_procedure_types]
    if not symptoms or not procedures:
        return doc, 0

    pool = symptoms + procedures
    tokens = {e["entity_id"]: _salient_tokens(e.get("display_name")) for e in pool}
    df = {}
    for e in pool:
        for w in tokens[e["entity_id"]]:
            df[w] = df.get(w, 0) + 1
    n = len(pool)
    max_df = max(2, int(ubiquity_ratio * n))
    discriminating = {w for w, c in df.items() if 2 <= c <= max_df}

    def shared_count(a, b):
        return len(tokens[a] & tokens[b] & discriminating)

    existing_pairs = {
        (r.get("source_entity_id"), r.get("target_entity_id")) for r in relationships
    }
    new_rels = []
    for s in symptoms:
        sid = s["entity_id"]
        ranked = sorted(
            ((shared_count(sid, p["entity_id"]), p) for p in procedures),
            key=lambda x: -x[0],
        )
        for ov, proc in ranked[:top_k]:
            if ov < min_shared:
                continue
            pid = proc["entity_id"]
            rel_type = (
                config.rca_diagnosed_by_relationship
                if is_diagnostic_procedure(proc.get("display_name"))
                else config.rca_remediated_by_relationship
            )
            if sid == pid or (sid, pid) in existing_pairs:
                continue
            existing_pairs.add((sid, pid))
            new_rels.append(
                _inferred_scr_relationship(
                    rel_type,
                    s,
                    proc,
                    "densify:diagnostic-path-" + rel_type,
                    confidence=_RCA_CONFIDENCE,
                )
            )

    relationships.extend(new_rels)
    doc["relationships"] = relationships
    return doc, len(new_rels)



# ---------------------------------------------------------------------------
# Configured umbrella-parent child rollup
# ---------------------------------------------------------------------------
#
# After the reading-order pass attaches children to fragment parents, this pass
# can roll those children up to a configured umbrella parent by shared key noun.

_ROLLUP_CONFIDENCE = 0.45
_ROLLUP_STOPWORDS = set(
    "the a an of to and or for with this that process replacement remove install "
    "removal installation procedure step steps guide new old".split()
)


def is_umbrella_procedure(display_name, patterns: tuple[str, ...] | None = None):
    """True if *display_name* matches a configured umbrella naming pattern."""
    return any(
        re.search(pattern, display_name or "", re.IGNORECASE)
        for pattern in (patterns or ())
    )


def _rollup_key_nouns(display_name):
    return {
        w for w in re.findall(r"[a-z0-9]+", (display_name or "").lower())
        if w not in _ROLLUP_STOPWORDS and len(w) >= 3
    }


def link_umbrella_steps(doc, config: DensifyConfig | None = None):
    """Roll fragment-procedure steps up to umbrella procedures by key-noun.

    For each umbrella procedure with no steps of its own, links it (has_step) to
    every Step owned by a fragment procedure in the same document that shares a
    key noun. Deterministic, idempotent, non-destructive. Returns ``(doc, added)``.
    """
    entities = doc.get("entities") or []
    relationships = doc.get("relationships") or []
    config = config or DEFAULT_DENSIFY_CONFIG
    procedures = [e for e in entities if e.get("entity_type") in config.procedure_types]
    if not procedures:
        return doc, 0

    has_step = {}
    for r in relationships:
        if r.get("relationship_type") == config.procedure_step_relationship:
            has_step.setdefault(r.get("source_entity_id"), set()).add(r.get("target_entity_id"))

    umbrellas = [
        p for p in procedures
        if is_umbrella_procedure(p.get("display_name"), config.umbrella_patterns) and not has_step.get(p["entity_id"])
    ]
    fragments = [
        p for p in procedures
        if has_step.get(p["entity_id"]) and not is_umbrella_procedure(p.get("display_name"), config.umbrella_patterns)
    ]
    if not umbrellas or not fragments:
        return doc, 0

    entity_by_id = {entity["entity_id"]: entity for entity in entities}
    frag_nouns = {f["entity_id"]: _rollup_key_nouns(f.get("display_name")) for f in fragments}

    existing_pairs = {
        (r.get("source_entity_id"), r.get("target_entity_id")) for r in relationships
    }
    new_rels = []
    for u in umbrellas:
        kn = _rollup_key_nouns(u.get("display_name"))
        if not kn:
            continue
        uid = u["entity_id"]
        for f in fragments:
            if not (kn & frag_nouns[f["entity_id"]]):
                continue
            for sid in has_step.get(f["entity_id"], ()):
                if uid == sid or (uid, sid) in existing_pairs:
                    continue
                existing_pairs.add((uid, sid))
                new_rels.append(
                    _inferred_scr_relationship(
                        config.procedure_step_relationship,
                        u,
                        entity_by_id[sid],
                        "densify:umbrella-step-rollup",
                        confidence=_ROLLUP_CONFIDENCE,
                    )
                )

    relationships.extend(new_rels)
    doc["relationships"] = relationships
    return doc, len(new_rels)
