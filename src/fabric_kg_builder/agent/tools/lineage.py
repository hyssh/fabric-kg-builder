"""agent/tools/lineage.py — safe lineage_trace tool for the grounded agent.

Returns source metadata (asset IDs, run IDs, timestamps, media types) for
the provenance of a retrieved chunk or entity.

SECURITY CONTRACT
-----------------
  • Never returns connection strings, account keys, SAS tokens, API keys,
    passwords, or any other credential.
  • Never returns raw file contents.
  • Only metadata fields listed in LineageSourceMetadata are returned.
  • The tool operates read-only against the local registry; it cannot modify
    any lineage records.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_REDACTED = "[redacted]"

# Fields that must always be redacted even if present in the source row.
_REDACT_KEYS = frozenset({
    "connection_string",
    "account_key",
    "sas_token",
    "api_key",
    "password",
    "secret",
    "token",
    "credential",
    "access_key",
    "private_key",
    "client_secret",
    "bearer",
})


@dataclass(frozen=True)
class LineageSourceMetadata:
    """Safe, non-secret metadata about the origin of a retrieved item.

    All fields are strings; no credentials or raw file bytes are included.
    """

    asset_id: str
    asset_version_id: str
    run_id: str
    original_name: str
    media_type: str
    source_uri_redacted: str  # scheme://.../<name> — no keys or SAS tokens
    registered_at: str
    pipeline_version: str = ""
    environment: str = ""
    content_hash_prefix: str = ""  # first 8 chars of SHA256, not the full hash


class SafeLineageTool:
    """Read-only lineage metadata tool — safe for inclusion in agent responses.

    Parameters
    ----------
    _registry_loader:
        Callable that returns the raw registry store dict (``{"asset_versions": [...], ...}``).
        Injected for testability.  If None the tool returns empty results.
    """

    def __init__(
        self,
        *,
        _registry_loader: Any | None = None,
    ) -> None:
        self._registry_loader = _registry_loader

    def get_source_metadata(self, asset_version_id: str) -> LineageSourceMetadata | None:
        """Return safe metadata for *asset_version_id*, or None if not found.

        No credentials are returned regardless of what the store contains.
        """
        if self._registry_loader is None:
            return None
        try:
            store = self._registry_loader()
        except Exception:
            return None

        version_row = self._find_version(store, asset_version_id)
        if version_row is None:
            return None

        asset_row = self._find_asset(store, version_row.get("asset_id", ""))
        run_row = self._find_run(store, version_row.get("run_id", ""))

        return LineageSourceMetadata(
            asset_id=str(version_row.get("asset_id", "")),
            asset_version_id=str(version_row.get("asset_version_id", "")),
            run_id=str(version_row.get("run_id", "")),
            original_name=str(version_row.get("original_name", "")),
            media_type=str(version_row.get("media_type", "")),
            source_uri_redacted=_redact_uri(str(version_row.get("source_uri", ""))),
            registered_at=str(version_row.get("registered_at", "")),
            pipeline_version=str(run_row.get("pipeline_version", "")) if run_row else "",
            environment=str(version_row.get("environment", "")),
            content_hash_prefix=str(version_row.get("content_hash", ""))[:8],
        )

    def _find_version(self, store: dict, avid: str) -> dict | None:
        for v in store.get("asset_versions", []):
            if v.get("asset_version_id") == avid:
                return _redact_row(v)
        return None

    def _find_asset(self, store: dict, aid: str) -> dict | None:
        for a in store.get("assets", []):
            if a.get("asset_id") == aid:
                return _redact_row(a)
        return None

    def _find_run(self, store: dict, run_id: str) -> dict | None:
        for r in store.get("processing_runs", []):
            if r.get("run_id") == run_id:
                return _redact_row(r)
        return None


def _redact_row(row: dict) -> dict:
    """Return a copy of *row* with all credential-like fields redacted."""
    result = {}
    for k, v in row.items():
        if k.lower() in _REDACT_KEYS:
            result[k] = _REDACTED
        else:
            result[k] = v
    return result


def _redact_uri(uri: str) -> str:
    """Redact credentials from a URI.  Removes query strings and SAS tokens."""
    if not uri:
        return ""
    # Remove query string (SAS tokens, account keys often appear there).
    if "?" in uri:
        uri = uri.split("?", 1)[0] + "?[redacted]"
    # Remove userinfo portion (http://user:pass@host).
    if "://" in uri:
        scheme, rest = uri.split("://", 1)
        if "@" in rest:
            rest = rest.split("@", 1)[1]
            uri = f"{scheme}://{rest}"
    return uri
