"""Canonical JSON hashing shared by schema-2 compile and deployment."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

SET_LIKE_FIELDS = frozenset({
    "aliases",
    "search_aliases",
    "evidence_ids",
    "evidence_id_hints",
    "source_span_ids",
    "reason_codes",
    "rejection_reasons",
    "audit_reason_codes",
    "audit_reasons",
    "description_evidence_id_hints",
    "cannot_link_keys",
})


def _utc(value: datetime) -> str:
    normalized = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return normalized.isoformat().replace("+00:00", "Z")


def canonicalize(value: Any, *, field_name: str = "") -> Any:
    """Normalize values exactly as the layer-4 schema-2 receipt does."""
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, float) and not math.isfinite(value):
        return {"__invalid_non_finite_number__": repr(value)}
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for original_key, item in sorted(
            value.items(), key=lambda pair: str(pair[0])
        ):
            key = str(original_key)
            if key.endswith("_json") and isinstance(item, str):
                try:
                    parsed = json.loads(item)
                except (json.JSONDecodeError, TypeError):
                    parsed = item
                normalized[key] = canonicalize(parsed, field_name=key)
            else:
                normalized[key] = canonicalize(item, field_name=key)
        return normalized
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [canonicalize(item) for item in value]
        if field_name in SET_LIKE_FIELDS or isinstance(value, (set, frozenset)):
            by_json = {canonical_json(item): item for item in items}
            return [by_json[key] for key in sorted(by_json)]
        return items
    return value


def canonical_json(value: Any) -> str:
    """Serialize one value to deterministic schema-2 canonical JSON."""
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: Any) -> str:
    """Return a prefixed SHA-256 over canonical JSON bytes."""
    payload = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_row_hash(row: Mapping[str, Any]) -> str:
    """Hash one canonical row."""
    return canonical_hash(dict(row))


def canonical_table_hash(
    rows: Sequence[Mapping[str, Any]],
    *primary_keys: str,
) -> str:
    """Hash canonical rows sorted by stable primary key and row hash."""
    decorated = [
        (
            tuple(str(row.get(key) or "") for key in primary_keys),
            canonical_row_hash(row),
            canonicalize(row),
        )
        for row in rows
    ]
    decorated.sort(key=lambda item: (item[0], item[1]))
    return canonical_hash([item[2] for item in decorated])


def arrow_schema_hash(schema: Any) -> str:
    """Hash an Arrow schema without relying on its display formatting."""
    return canonical_hash([
        {
            "name": field.name,
            "type": str(field.type),
            "nullable": bool(field.nullable),
        }
        for field in schema
    ])


def arrow_table_hash(table: Any, *primary_keys: str) -> str:
    """Hash an Arrow table with the shared canonical row algorithm."""
    return canonical_table_hash(table.to_pylist(), *primary_keys)
