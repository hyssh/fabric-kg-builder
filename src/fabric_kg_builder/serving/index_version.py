"""serving.index_version — deterministic physical index version naming.

M6 SRV-003/004: physical index names are derived from a SHA-256 fingerprint of:
  * the index schema (fields, vectorSearch config)
  * the embedding model name
  * the embedding dimensions

The fingerprint is truncated to 8 hex chars to form a stable, human-readable
physical name suffix.  Aliases are stable names that point at a versioned index.

Immutability contract (SRV-004):
  - Once a versioned index exists, its embedding model and dimensions are immutable.
  - Any attempt to push docs to a version with a different model/dims raises
    ``EmbeddingMismatchError`` before any upload begins.

Usage::

    from fabric_kg_builder.serving.index_version import (
        physical_index_name,
        stable_alias,
        compute_index_fingerprint,
        assert_embedding_match,
        EmbeddingMismatchError,
    )

    fp = compute_index_fingerprint(schema_dict, "text-embedding-3-large", 1536)
    # e.g. "a3f8e901"

    name = physical_index_name("kg-chunks", fp)
    # e.g. "kg-chunks-v-a3f8e901"

    alias = stable_alias("kg-chunks")
    # e.g. "kg-chunks"   (alias points to the live versioned index)

    assert_embedding_match(schema_dict, "text-embedding-3-large", 1536, stored_fp="a3f8e901")
    # raises EmbeddingMismatchError if the fingerprint does not match
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


class EmbeddingMismatchError(ValueError):
    """Raised when the embedding model or dimensions mismatch a versioned index."""


def compute_index_fingerprint(
    schema_dict: dict[str, Any],
    embedding_model: str,
    dimensions: int,
) -> str:
    """Compute a deterministic 8-char hex fingerprint for a versioned index.

    The fingerprint is derived from SHA-256 of:
      - the canonical JSON of schema_dict["fields"] + schema_dict.get("vectorSearch")
      - the embedding model name
      - the dimensions

    ``"_"``-prefixed keys (comments, sprint tags) are excluded to keep the
    fingerprint stable across documentation-only changes.

    Parameters
    ----------
    schema_dict:
        Raw index schema dict (fields list + optional vectorSearch).
    embedding_model:
        Embedding model name (e.g. "text-embedding-3-large").
    dimensions:
        Embedding vector dimensions (e.g. 1536).

    Returns
    -------
    str
        8-character lowercase hex string.
    """
    # Isolate the schema-structural parts that matter for the index shape.
    canonical = {
        "fields": _strip_underscore_keys(schema_dict.get("fields", [])),
        "vectorSearch": _strip_underscore_keys(schema_dict.get("vectorSearch", {})),
        "embedding_model": embedding_model,
        "dimensions": dimensions,
    }
    raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def physical_index_name(base_name: str, fingerprint: str) -> str:
    """Return the versioned physical index name.

    E.g. ``physical_index_name("kg-chunks", "a3f8e901")`` → ``"kg-chunks-v-a3f8e901"``.
    """
    return f"{base_name}-v-{fingerprint}"


def stable_alias(base_name: str) -> str:
    """Return the stable alias name for a base index name.

    The alias is identical to the base name, pointing to the active physical
    version.  E.g. ``stable_alias("kg-chunks")`` → ``"kg-chunks"``.
    """
    return base_name


def assert_embedding_match(
    schema_dict: dict[str, Any],
    embedding_model: str,
    dimensions: int,
    stored_fingerprint: str,
) -> None:
    """Raise EmbeddingMismatchError if schema+model+dims produce a different fingerprint.

    This check must run BEFORE any document upload to prevent partial-version
    corruption.

    Parameters
    ----------
    schema_dict:
        Current schema being deployed.
    embedding_model:
        Embedding model name to verify.
    dimensions:
        Embedding dimensions to verify.
    stored_fingerprint:
        The fingerprint recorded when the versioned index was first created.

    Raises
    ------
    EmbeddingMismatchError
        If the computed fingerprint differs from ``stored_fingerprint``.
    """
    computed = compute_index_fingerprint(schema_dict, embedding_model, dimensions)
    if computed != stored_fingerprint:
        raise EmbeddingMismatchError(
            f"Embedding mismatch: stored fingerprint '{stored_fingerprint}' does not match "
            f"computed '{computed}' for model='{embedding_model}', dims={dimensions}. "
            "Embedding model/dimensions are immutable per versioned index. "
            "Deploy to a new version instead of modifying an existing one."
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_underscore_keys(obj: Any) -> Any:
    """Recursively remove all keys starting with ``'_'`` from dicts."""
    if isinstance(obj, dict):
        return {
            k: _strip_underscore_keys(v)
            for k, v in obj.items()
            if not k.startswith("_")
        }
    if isinstance(obj, list):
        return [_strip_underscore_keys(item) for item in obj]
    return obj
