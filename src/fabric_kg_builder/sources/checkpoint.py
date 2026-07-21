"""Checkpoint fingerprint and replay helpers (EXT-008).

A checkpoint key is a deterministic SHA-256 hash of:
  - the source file's content hash (from ``SourceFileRow.content_hash``)
  - the adapter name
  - the adapter version (from ``ADAPTER_CONTRACT_VERSION`` or adapter-specific)
  - the extraction options dict (JSON-serialised, keys sorted)

This allows callers to detect that a source file has already been processed
with the same adapter version and options, enabling skip-on-replay.
"""

from __future__ import annotations

import hashlib
import json
import threading
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapter import AdapterError, FailureType


# ---------------------------------------------------------------------------
# Fingerprint helpers
# ---------------------------------------------------------------------------


def compute_checkpoint_fingerprint(
    content_hash: str,
    adapter_name: str,
    adapter_version: str,
    options: dict[str, Any] | None = None,
) -> str:
    """Return a deterministic hex fingerprint for checkpoint/replay.

    Parameters
    ----------
    content_hash:
        SHA-256 of the source file bytes (``SourceFileRow.content_hash``).
    adapter_name:
        Stable adapter identifier, e.g. ``"parquet_adapter"``.
    adapter_version:
        Adapter version string, e.g. ``"1.0.0"``.
    options:
        Optional dict of extraction options (e.g. ``{"max_rows": 1000}``).
        Keys are sorted before hashing so insertion order does not matter.

    Returns
    -------
    str
        64-character lowercase hex SHA-256 digest.
    """
    payload = {
        "content_hash": content_hash,
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
        "options": options or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Checkpoint record
# ---------------------------------------------------------------------------


@dataclass
class CheckpointRecord:
    """Persisted checkpoint entry for one extraction run."""

    fingerprint: str
    content_hash: str
    adapter_name: str
    adapter_version: str
    options: dict[str, Any]
    source_locator: str
    completed_at: str  # ISO-8601 UTC


# ---------------------------------------------------------------------------
# Checkpoint store (simple JSON file)
# ---------------------------------------------------------------------------


class CheckpointStore:
    """Read/write checkpoint records from a single JSON file.

    Parameters
    ----------
    store_path:
        Path to the JSON checkpoint file.  Created on first write.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        if store_path.exists():
            try:
                raw = json.loads(store_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._records = raw
                else:
                    raise AdapterError(
                        FailureType.CORRUPT,
                        f"Checkpoint file '{store_path}' is not a JSON object.",
                        str(store_path),
                    )
            except json.JSONDecodeError as exc:
                raise AdapterError(
                    FailureType.CORRUPT,
                    f"Checkpoint file '{store_path}' contains invalid JSON: {exc}",
                    str(store_path),
                ) from exc
            except OSError as exc:
                raise AdapterError(
                    FailureType.CORRUPT,
                    f"Cannot read checkpoint file '{store_path}': {exc}",
                    str(store_path),
                ) from exc

    # ------------------------------------------------------------------

    def has(self, fingerprint: str) -> bool:
        """Return True when *fingerprint* was recorded as completed."""
        with self._lock:
            return fingerprint in self._records

    def record(
        self,
        content_hash: str,
        adapter_name: str,
        adapter_version: str,
        options: dict[str, Any] | None,
        source_locator: str,
    ) -> CheckpointRecord:
        """Compute the fingerprint, persist the record, and return it."""
        fp = compute_checkpoint_fingerprint(
            content_hash, adapter_name, adapter_version, options
        )
        entry: dict[str, Any] = {
            "fingerprint": fp,
            "content_hash": content_hash,
            "adapter_name": adapter_name,
            "adapter_version": adapter_version,
            "options": options or {},
            "source_locator": source_locator,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._records[fp] = entry
            self._flush()
        return CheckpointRecord(**entry)

    def lookup(self, fingerprint: str) -> CheckpointRecord | None:
        """Return the record for *fingerprint*, or None if not found."""
        with self._lock:
            raw = self._records.get(fingerprint)
        if raw is None:
            return None
        return CheckpointRecord(**raw)

    def persist(self) -> None:
        """Persist current state without adding a completion record."""
        with self._lock:
            self._flush()

    # ------------------------------------------------------------------

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_name(f".tmp-{uuid4().hex[:16]}.json")
        temp_path.write_text(
            json.dumps(self._records, indent=2, default=str),
            encoding="utf-8",
        )
        temp_path.replace(self._path)
