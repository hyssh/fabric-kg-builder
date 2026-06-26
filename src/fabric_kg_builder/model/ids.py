"""Deterministic ID generation for all 8 canonical Parquet tables.

All IDs are stable SHA-256-derived strings with a typed prefix so they are
visually scoped and debuggable.  Same inputs always produce the same ID —
no UUIDs, no runtime state.  See SPEC-002 §5 for the full specification.
"""

from __future__ import annotations

import hashlib
import re


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def make_id(prefix: str, canonical_string: str) -> str:
    """Return ``prefix:sha256(canonical_string)[:32]``."""
    digest = hashlib.sha256(canonical_string.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:32]}"


def content_hash(text: str) -> str:
    """Full SHA-256 hex digest of *text* — used for dedup and change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# canonical_key normalization  (SPEC-002 §5.2 — pinned; changes need spec bump)
# ---------------------------------------------------------------------------


def normalize_canonical_key(entity_type: str, display_name: str) -> str:
    """Normalise an entity into its stable identity key.

    Rules (in order):
    1. Lowercase the display name.
    2. Strip leading/trailing whitespace.
    3. Collapse internal whitespace runs to single space.
    4. Remove all non-alphanumeric characters except ``-`` and space.
    5. Replace spaces with ``-``.
    6. Prepend ``entity_type.lower():`` separated by ``:``.

    Examples::

        normalize_canonical_key("Device", "Surface Laptop 5")
        # → "device:surface-laptop-5"

        normalize_canonical_key("Component", "Battery Pack")
        # → "component:battery-pack"

        normalize_canonical_key("PartNumber", "M1287099-003")
        # → "partnumber:m1287099-003"
    """
    name = display_name.lower().strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"[^a-z0-9\- ]", "", name)
    name = name.replace(" ", "-")
    return f"{entity_type.lower()}:{name}"


# ---------------------------------------------------------------------------
# Per-table ID constructors
# ---------------------------------------------------------------------------


def make_entity_id(entity_type: str, display_name: str) -> str:
    """Stable entity_id derived from canonical_key (SPEC-002 §5.2)."""
    canonical_key = normalize_canonical_key(entity_type, display_name)
    return make_id("entity", canonical_key)


def make_source_file_id(canonical_path: str, content_hash_value: str) -> str:
    """Stable source_file_id (SPEC-002 §5.9).

    *canonical_path*: forward-slash-normalised relative path from project root.
    """
    return make_id("src", f"{canonical_path}:{content_hash_value}")


def make_document_element_id(
    source_file_id: str,
    element_type: str,
    page: int | None,
    sort_order: int | None,
    content_hash_value: str,
) -> str:
    """Stable document_element_id (SPEC-002 §5.3)."""
    parts = [
        source_file_id,
        element_type,
        str(page or ""),
        str(sort_order or ""),
        content_hash_value[:16],
    ]
    return make_id("elem", ":".join(parts))


def make_chunk_id(source_file_id: str, chunk_type: str, content_hash_value: str) -> str:
    """Stable chunk_id (SPEC-002 §5.4)."""
    return make_id("chunk", f"{source_file_id}:{chunk_type}:{content_hash_value}")


def make_relationship_id(
    relationship_type: str,
    source_entity_id: str,
    target_entity_id: str,
) -> str:
    """Stable relationship_id (SPEC-002 §5.5).

    Multiple evidence records may support the same logical relationship; the
    relationship row is deduplicated by this triple.
    """
    return make_id("rel", f"{relationship_type}:{source_entity_id}:{target_entity_id}")


def make_evidence_id(
    source_file_id: str,
    source_type: str,
    context_key: str,
    text_hash_value: str,
) -> str:
    """Stable evidence_id (SPEC-002 §5.6).

    *context_key* encodes page+row+col+element IDs as available.
    """
    return make_id("evid", f"{source_file_id}:{source_type}:{context_key}:{text_hash_value[:16]}")


def make_image_id(source_file_id: str, image_hash_value: str) -> str:
    """Stable image_id (SPEC-002 §5.7).

    Identical images from different source files get different IDs because
    source_file_id differs; shared image_hash enables cross-source dedup.
    """
    return make_id("img", f"{source_file_id}:{image_hash_value}")


def make_visual_region_id(
    image_id: str,
    region_type: str,
    label: str | None,
    sort_index: int,
) -> str:
    """Stable visual_region_id (SPEC-002 §5.8)."""
    return make_id("vr", f"{image_id}:{region_type}:{label or ''}:{sort_index}")
