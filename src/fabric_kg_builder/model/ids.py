"""Deterministic IDs plus lineage v2 UUID helpers."""

from __future__ import annotations

import hashlib
import re
import uuid


_LINEAGE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "fabric-kg-builder/lineage-v2")
_MIGRATION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "fabric-kg-builder/migration-v2")


def make_id(prefix: str, canonical_string: str) -> str:
    """Return ``prefix:sha256(canonical_string)[:32]``."""
    digest = hashlib.sha256(canonical_string.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:32]}"


def content_hash(text: str) -> str:
    """Full SHA-256 hex digest of *text* — used for dedup and change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_canonical_key(
    entity_type: str,
    display_name: str,
    identity_context: str | None = None,
) -> str:
    """Normalise an entity into its stable identity key."""
    name = display_name.lower().strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"[^a-z0-9\- ]", "", name)
    name = name.replace(" ", "-")
    key = f"{entity_type.lower()}:{name}"
    if identity_context:
        normalized_context = re.sub(
            r"\s+",
            " ",
            identity_context.strip().casefold(),
        )
        key = f"{key}:ctx-{hashlib.sha256(normalized_context.encode('utf-8')).hexdigest()[:16]}"
    return key


def make_entity_id(
    entity_type: str,
    display_name: str,
    identity_context: str | None = None,
) -> str:
    canonical_key = normalize_canonical_key(
        entity_type,
        display_name,
        identity_context,
    )
    return make_id("entity", canonical_key)


def make_source_file_id(canonical_path: str, content_hash_value: str) -> str:
    return make_id("src", f"{canonical_path}:{content_hash_value}")


def make_document_element_id(
    source_file_id: str,
    element_type: str,
    page: int | None,
    sort_order: int | None,
    content_hash_value: str,
) -> str:
    parts = [
        source_file_id,
        element_type,
        str(page or ""),
        str(sort_order or ""),
        content_hash_value[:16],
    ]
    return make_id("elem", ":".join(parts))


def make_chunk_id(source_file_id: str, chunk_type: str, content_hash_value: str) -> str:
    return make_id("chunk", f"{source_file_id}:{chunk_type}:{content_hash_value}")


def make_relationship_id(
    relationship_type: str,
    source_entity_id: str,
    target_entity_id: str,
    identity_context: str | None = None,
) -> str:
    payload = f"{relationship_type}:{source_entity_id}:{target_entity_id}"
    if identity_context:
        payload = f"{payload}:{identity_context}"
    return make_id("rel", payload)


def make_property_observation_id(
    entity_id: str,
    property_id: str,
    normalized_value_json: str,
    observed_at: str | None,
) -> str:
    """Return a fact-stable ID independent of overlapping evidence windows."""
    return make_id(
        "propobs",
        f"{entity_id}:{property_id}:{normalized_value_json}:{observed_at or ''}",
    )


def make_property_conflict_id(
    entity_id: str,
    property_id: str,
    temporal_key: str,
) -> str:
    """Return a deterministic ID for disagreeing values in one fact slot."""
    return make_id(
        "propconflict",
        f"{entity_id}:{property_id}:{temporal_key}",
    )


def make_evidence_id(
    source_file_id: str,
    source_type: str,
    context_key: str,
    text_hash_value: str,
) -> str:
    return make_id("evid", f"{source_file_id}:{source_type}:{context_key}:{text_hash_value[:16]}")


def make_image_id(source_file_id: str, image_hash_value: str) -> str:
    return make_id("img", f"{source_file_id}:{image_hash_value}")


def make_visual_region_id(
    image_id: str,
    region_type: str,
    label: str | None,
    sort_index: int,
) -> str:
    return make_id("vr", f"{image_id}:{region_type}:{label or ''}:{sort_index}")


# ---------------------------------------------------------------------------
# Lineage v2 helpers
# ---------------------------------------------------------------------------


def make_asset_id() -> str:
    """Mint a new logical asset identifier (UUIDv4)."""
    return str(uuid.uuid4())


def make_asset_version_id() -> str:
    """Mint a new asset version identifier (UUIDv4)."""
    return str(uuid.uuid4())


def make_run_id() -> str:
    """Mint a new processing run identifier (UUIDv4)."""
    return str(uuid.uuid4())


def make_deployment_id() -> str:
    """Mint a new deployment identifier (UUIDv4)."""
    return str(uuid.uuid4())


def make_asset_version_identity(asset_id: str, content_hash_value: str) -> str:
    """Deterministic version identity used for idempotent registration lookup."""
    return hashlib.sha256(f"{asset_id}:{content_hash_value}".encode("utf-8")).hexdigest()


def make_element_lineage_id(asset_version_id: str, adapter_path: str) -> str:
    """Deterministic UUIDv5 for adapter-emitted element identity."""
    return str(uuid.uuid5(_LINEAGE_NAMESPACE, f"element:{asset_version_id}:{adapter_path}"))


def make_chunk_lineage_id(
    element_id: str,
    strategy_version: str,
    ordinal: int,
    content_hash_value: str,
) -> str:
    """Deterministic UUIDv5 for strategy-specific chunk identity."""
    return str(
        uuid.uuid5(
            _LINEAGE_NAMESPACE,
            f"chunk:{element_id}:{strategy_version}:{ordinal}:{content_hash_value}",
        )
    )


def make_migrated_asset_id(legacy_source_file_id: str) -> str:
    """Deterministic migration ID so repeated migrations preserve stable assets."""
    return str(uuid.uuid5(_MIGRATION_NAMESPACE, f"asset:{legacy_source_file_id}"))


def make_migrated_asset_version_id(asset_id: str, content_hash_value: str) -> str:
    """Deterministic migration version ID keyed by migrated asset + content hash."""
    return str(uuid.uuid5(_MIGRATION_NAMESPACE, f"asset-version:{asset_id}:{content_hash_value}"))


def make_migrated_run_id(scope: str) -> str:
    """Deterministic migration run ID for legacy backfills."""
    return str(uuid.uuid5(_MIGRATION_NAMESPACE, f"run:{scope}"))


def looks_like_uuid(value: str) -> bool:
    """Return True when *value* parses as a UUID string."""
    try:
        uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True
