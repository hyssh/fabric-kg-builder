"""densify.py — deterministic graph densification for the knowledge graph.

Entity/relationship extraction runs per document section, so the resulting graph
is full of islands: a `DeviceModel` node (e.g. "Surface Laptop 5") often has no
edges to the Components/Parts/Procedures described in the *same* service guide,
so data-agent queries like "parts for the Surface Laptop 5" return nothing.

This module adds **source-document hub edges**: within each enriched document, it
links the device model(s) that document covers to the Component / Part /
Procedure / Symptom entities in that same document. The links are deterministic
(stable IDs), idempotent (existing pairs are never duplicated), and reversible
(written to a new directory — the source enriched files are untouched).

It does NOT call an LLM and does NOT re-run extraction.

In addition to the DeviceModel hub edges, :func:`link_symptom_cause_resolution`
connects troubleshooting triples (Cause → Symptom → Resolution) that the
per-section extraction left isolated, using document-scoped keyword overlap
gated by token specificity (ubiquitous tokens like "battery" in a battery guide
are ignored), plus a high-precision transitive ``Cause → Resolution`` shortcut.
These associative edges carry a lower confidence (0.45) and an ``origin`` tag so
they are auditable and removable.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from fabric_kg_builder.model.ids import content_hash, make_relationship_id

logger = logging.getLogger(__name__)

# DeviceModel → target-type hub relationship verbs.  These reuse the dominant
# verbs already present in the data so the multi-type ontology planner folds the
# new edges into the existing (DeviceModel → X) typed relationships.
HUB_RELATION_BY_TARGET: dict[str, str] = {
    "Component": "has_component",
    "Part": "has_part",
    "Procedure": "has_procedure",
    "Symptom": "has_symptom",
}

# Product keywords that mark a DeviceModel name as *specific* (vs generic like
# "model" / "this device model" / "device").
_PRODUCT_KEYWORDS = ("surface", "laptop", "pro", "studio", "go", "book", "hub")

# Generic device-model names to never treat as a real hub.
_GENERIC_NAMES = {
    "model", "models", "device", "devices", "this device model",
    "device model", "unit", "units", "product", "products",
}


def is_specific_device_model(display_name: str | None) -> bool:
    """Return True if *display_name* looks like a concrete Surface model.

    A specific model contains a product keyword AND a distinguishing token
    (a digit or the word "edition").  This filters out generic placeholders
    like "model" or "this device model" that would create noisy hub links.
    """
    if not display_name:
        return False
    name = display_name.strip().lower()
    if name in _GENERIC_NAMES:
        return False
    if not any(k in name for k in _PRODUCT_KEYWORDS):
        return False
    has_digit = bool(re.search(r"\d", name))
    has_edition = "edition" in name
    return has_digit or has_edition


def _new_hub_relationship(
    rel_type: str, source_entity_id: str, target_entity_id: str
) -> dict[str, Any]:
    return {
        "relationship_id": make_relationship_id(rel_type, source_entity_id, target_entity_id),
        "relationship_type": rel_type,
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "evidence_id": None,
        "properties_json": '{"origin":"densify:source-model-hub"}',
        "confidence": 0.5,
        "is_placeholder": False,
        "content_hash": content_hash(f"{rel_type}:{source_entity_id}:{target_entity_id}"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def densify_document(
    doc: dict[str, Any], max_models: int = 5
) -> tuple[dict[str, Any], int]:
    """Add DeviceModel→entity hub edges to one enriched document.

    Parameters
    ----------
    doc:
        A parsed enriched canonical document (one source file) with
        ``entities`` and ``relationships`` lists.
    max_models:
        Cap on the number of specific device models to use as hubs per document
        (guards against pathological docs that mention many models).

    Returns
    -------
    (doc, added):
        The same ``doc`` dict (mutated in place) and the count of new edges.
    """
    entities = doc.get("entities") or []
    relationships = doc.get("relationships") or []

    # Specific device models in this document, ranked by name length (longer ⇒
    # more specific), capped.
    models = [
        e for e in entities
        if e.get("entity_type") == "DeviceModel"
        and is_specific_device_model(e.get("display_name"))
    ]
    models.sort(key=lambda e: len(e.get("display_name") or ""), reverse=True)
    models = models[:max_models]
    if not models:
        return doc, 0

    model_ids = [m["entity_id"] for m in models]

    # Existing (source, target) pairs — never duplicate.
    existing_pairs: set[tuple[str, str]] = {
        (r.get("source_entity_id"), r.get("target_entity_id")) for r in relationships
    }

    # Targets: linkable entities of the hub-eligible types in this document.
    new_rels: list[dict[str, Any]] = []
    for ent in entities:
        rel_type = HUB_RELATION_BY_TARGET.get(ent.get("entity_type"))
        if rel_type is None:
            continue
        tgt = ent["entity_id"]
        for mid in model_ids:
            if mid == tgt:
                continue
            if (mid, tgt) in existing_pairs:
                continue
            existing_pairs.add((mid, tgt))
            new_rels.append(_new_hub_relationship(rel_type, mid, tgt))

    relationships.extend(new_rels)
    doc["relationships"] = relationships
    return doc, len(new_rels)


# ---------------------------------------------------------------------------
# Symptom ↔ Cause ↔ Resolution linking (troubleshooting triples)
# ---------------------------------------------------------------------------

# Tokens too generic to be a useful linking signal.  Domain words like
# "battery"/"surface" are added dynamically per-document (ubiquity gate), but
# these are always dropped.
_SCR_STOPWORDS = set(
    "the a an and or of to in on for with without your you this that these those is "
    "are be by from at as it its their them they we our using use used into not no "
    "non device devices surface microsoft guide service should must may can will if "
    "when before after during while replace replacement install installation remove "
    "removal also other more most some any all per via due such only than then them "
    "this that have has had been being which what when where how who whom each".split()
)

# Confidence assigned to inferred (associative) S/C/R edges — lower than
# extracted edges so downstream consumers can distinguish them.
_SCR_CONFIDENCE = 0.45

# Cause→Symptom and Symptom→Resolution verbs (reuse existing dominant verbs).
_SCR_CAUSE_SYMPTOM = "causes"
_SCR_SYMPTOM_RESOLUTION = "resolved_by"
_SCR_CAUSE_RESOLUTION = "addressed_by"


def _salient_tokens(display_name: str | None) -> set[str]:
    """Significant lowercase tokens from *display_name* (len ≥ 4, not stopwords)."""
    return {
        w
        for w in re.findall(r"[a-z0-9]+", (display_name or "").lower())
        if len(w) >= 4 and w not in _SCR_STOPWORDS
    }


def _inferred_scr_relationship(
    rel_type: str, source_entity_id: str, target_entity_id: str, origin: str,
    confidence: float = _SCR_CONFIDENCE,
) -> dict[str, Any]:
    return {
        "relationship_id": make_relationship_id(rel_type, source_entity_id, target_entity_id),
        "relationship_type": rel_type,
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "evidence_id": None,
        "properties_json": f'{{"origin":"{origin}"}}',
        "confidence": confidence,
        "is_placeholder": False,
        "content_hash": content_hash(f"{rel_type}:{source_entity_id}:{target_entity_id}"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def link_symptom_cause_resolution(
    doc: dict[str, Any],
    top_k: int = 3,
    ubiquity_ratio: float = 0.4,
    min_shared: int = 1,
) -> tuple[dict[str, Any], int]:
    """Link Cause→Symptom→Resolution triples within one enriched document.

    Uses document-scoped keyword overlap gated by token *specificity*: a token
    that appears in more than *ubiquity_ratio* of the document's S/C/R entities
    (e.g. "battery" in a battery guide) is ignored, so only discriminating tokens
    (e.g. "thermal", "venting", "kickstand") create links. For each Symptom, the
    top *top_k* Causes and Resolutions by shared-token count are linked. A
    high-precision transitive ``Cause → Resolution`` (``addressed_by``) edge is
    added wherever a linked Cause→Symptom→Resolution path results.

    Inferred edges carry confidence 0.45 and an ``origin`` tag. Deterministic,
    idempotent (existing pairs never duplicated), non-destructive.

    Returns ``(doc, added)``.
    """
    entities = doc.get("entities") or []
    relationships = doc.get("relationships") or []

    causes = [e for e in entities if e.get("entity_type") == "Cause"]
    symptoms = [e for e in entities if e.get("entity_type") == "Symptom"]
    resolutions = [e for e in entities if e.get("entity_type") == "Resolution"]
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

    def add(rel_type: str, src: str, tgt: str, origin: str) -> None:
        if src == tgt or (src, tgt) in existing_pairs:
            return
        existing_pairs.add((src, tgt))
        new_rels.append(_inferred_scr_relationship(rel_type, src, tgt, origin))

    # Symptom → its top causes / resolutions, plus transitive Cause → Resolution.
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
            add(_SCR_CAUSE_SYMPTOM, cid, sid, "densify:scr-keyword")
        for rid in linked_res:
            add(_SCR_SYMPTOM_RESOLUTION, sid, rid, "densify:scr-keyword")
        # Transitive Cause → Resolution (high precision: both share the symptom).
        for cid in linked_causes:
            for rid in linked_res:
                add(_SCR_CAUSE_RESOLUTION, cid, rid, "densify:scr-transitive")

    relationships.extend(new_rels)
    doc["relationships"] = relationships
    return doc, len(new_rels)


# ---------------------------------------------------------------------------
# Procedure → Step linking (by document reading order)
# ---------------------------------------------------------------------------
#
# Per-section extraction rarely emits has_step edges (only ~2% of procedures
# end up with any step in the raw graph), so "list the steps for procedure X"
# queries return nothing even though the Step entities exist. We reconstruct the
# links structurally: map each Procedure and Step entity to its position in the
# document (page_number, sort_order) via the document_elements text, then assign
# each Step to the nearest *preceding* Procedure in reading order.

_PROC_STEP_RELATION = "has_step"
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
) -> tuple[dict[str, Any], int]:
    """Link Step entities to their nearest preceding Procedure by reading order.

    Maps Procedure and Step entities to document positions using
    ``document_elements`` text, walks the merged sequence in reading order, and
    attaches each Step to the most recent Procedure seen (within
    :data:`_PROC_STEP_MAX_GAP` elements). Deterministic, idempotent,
    non-destructive; inferred edges carry confidence 0.5 and an origin tag.

    Returns ``(doc, added)``.
    """
    entities = doc.get("entities") or []
    relationships = doc.get("relationships") or []
    index = _build_element_index(doc.get("document_elements") or [])
    if not index:
        return doc, 0

    procedures = [e for e in entities if e.get("entity_type") == "Procedure"]
    steps = [e for e in entities if e.get("entity_type") == "Step"]
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
        new_rels.append({
            "relationship_id": make_relationship_id(_PROC_STEP_RELATION, cur_proc, eid),
            "relationship_type": _PROC_STEP_RELATION,
            "source_entity_id": cur_proc,
            "target_entity_id": eid,
            "evidence_id": None,
            "properties_json": '{"origin":"densify:proc-step-readingorder"}',
            "confidence": _PROC_STEP_CONFIDENCE,
            "is_placeholder": False,
            "content_hash": content_hash(f"{_PROC_STEP_RELATION}:{cur_proc}:{eid}"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    relationships.extend(new_rels)
    doc["relationships"] = relationships
    return doc, len(new_rels)


# ---------------------------------------------------------------------------
# RCA diagnostic-path linking (Symptom -> Procedure)
# ---------------------------------------------------------------------------
#
# The data already has Symptom<-Cause and Symptom->Resolution, but the link from
# a Symptom to the actionable Procedure (and its Steps) is essentially missing
# (only a handful of has_resolution edges). This linker closes that gap so a
# Symptom becomes the hub of a full root-cause-analysis answer:
#
#   Cause --causes--> Symptom --diagnosed_by--> Procedure[diagnostic]
#                       |  \--remediated_by--> Procedure[repair] --has_step--> Step
#                       \--resolved_by--> Resolution
#
# Procedures are classified as *diagnostic* (SDT / check / test / inspect /
# validate / verify / status) vs *repair* by name, then linked to symptoms in the
# same document by discriminating-keyword overlap (same ubiquity gate as the
# Cause/Symptom/Resolution linker).

_RCA_DIAGNOSED_BY = "diagnosed_by"      # Symptom -> diagnostic Procedure
_RCA_REMEDIATED_BY = "remediated_by"    # Symptom -> repair Procedure
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


def link_rca_paths(doc, top_k=3, ubiquity_ratio=0.4, min_shared=1):
    """Link each Symptom to its diagnostic and remediation Procedures.

    Within one document, scores Symptom/Procedure pairs by shared discriminating
    tokens (ubiquitous tokens like "battery" in a battery guide are ignored), and
    for the top *top_k* procedures per symptom adds ``diagnosed_by`` if the
    procedure name is diagnostic (:func:`is_diagnostic_procedure`) else
    ``remediated_by``. Reuses the Symptom/Cause/Resolution token model. Inferred
    edges carry confidence 0.4 and an origin tag. Deterministic, idempotent,
    non-destructive. Returns ``(doc, added)``.
    """
    entities = doc.get("entities") or []
    relationships = doc.get("relationships") or []

    symptoms = [e for e in entities if e.get("entity_type") == "Symptom"]
    procedures = [e for e in entities if e.get("entity_type") == "Procedure"]
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
                _RCA_DIAGNOSED_BY
                if is_diagnostic_procedure(proc.get("display_name"))
                else _RCA_REMEDIATED_BY
            )
            if sid == pid or (sid, pid) in existing_pairs:
                continue
            existing_pairs.add((sid, pid))
            new_rels.append(
                _inferred_scr_relationship(rel_type, sid, pid, "densify:rca-" + rel_type, confidence=_RCA_CONFIDENCE)
            )

    relationships.extend(new_rels)
    doc["relationships"] = relationships
    return doc, len(new_rels)



# ---------------------------------------------------------------------------
# Umbrella-procedure step rollup
# ---------------------------------------------------------------------------
#
# Named "X Replacement Process" procedures are what users ask for ("steps for the
# battery replacement"), but the steps were extracted under fragment procedures
# ("Remove the Battery", "Insert the Battery"). After link_procedure_steps() has
# attached steps to fragments, this pass rolls those steps up to the umbrella by
# shared key-noun within the same document, so the umbrella procedure exposes the
# full step set. Runs AFTER link_procedure_steps. Additive, idempotent.

_ROLLUP_RELATION = "has_step"
_ROLLUP_CONFIDENCE = 0.45
_UMBRELLA_RE = re.compile(r"replacement process$|\breplacement$|\bprocess$", re.IGNORECASE)
_ROLLUP_STOPWORDS = set(
    "the a an of to and or for with this that process replacement remove install "
    "removal installation procedure device devices surface microsoft step steps "
    "guide service new old".split()
)


def is_umbrella_procedure(display_name):
    """True if *display_name* looks like an umbrella 'X Replacement Process'."""
    return bool(_UMBRELLA_RE.search(display_name or ""))


def _rollup_key_nouns(display_name):
    return {
        w for w in re.findall(r"[a-z0-9]+", (display_name or "").lower())
        if w not in _ROLLUP_STOPWORDS and len(w) >= 3
    }


def link_umbrella_steps(doc):
    """Roll fragment-procedure steps up to umbrella procedures by key-noun.

    For each umbrella procedure with no steps of its own, links it (has_step) to
    every Step owned by a fragment procedure in the same document that shares a
    key noun. Deterministic, idempotent, non-destructive. Returns ``(doc, added)``.
    """
    entities = doc.get("entities") or []
    relationships = doc.get("relationships") or []
    procedures = [e for e in entities if e.get("entity_type") == "Procedure"]
    if not procedures:
        return doc, 0

    has_step = {}
    for r in relationships:
        if r.get("relationship_type") == "has_step":
            has_step.setdefault(r.get("source_entity_id"), set()).add(r.get("target_entity_id"))

    umbrellas = [
        p for p in procedures
        if is_umbrella_procedure(p.get("display_name")) and not has_step.get(p["entity_id"])
    ]
    fragments = [
        p for p in procedures
        if has_step.get(p["entity_id"]) and not is_umbrella_procedure(p.get("display_name"))
    ]
    if not umbrellas or not fragments:
        return doc, 0

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
                        _ROLLUP_RELATION, uid, sid, "densify:umbrella-step-rollup",
                        confidence=_ROLLUP_CONFIDENCE,
                    )
                )

    relationships.extend(new_rels)
    doc["relationships"] = relationships
    return doc, len(new_rels)
