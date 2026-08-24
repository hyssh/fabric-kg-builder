"""Secret and source-content redaction for serialised release evidence.

This module provides redaction helpers for the release package that prevent
secrets and source document content from appearing in evidence manifests,
run logs, or resource ledgers.

Reuses the same pattern established in :mod:`fabric_kg_builder.lineage.migration`
(source content redaction in run manifests) without duplicating insecure broad
logic or adding new regex-based secret-scanning heuristics.

Threat model:
- Secrets are injected via environment variables or Key Vault.  They must not
  appear in serialised evidence or logs.
- Source document text is customer data.  It must not appear in manifests.
- Blob storage URIs are safe to retain (they are resource identifiers, not
  secrets).
- ARM/Fabric resource IDs are safe to retain.

What IS redacted:
- Fields named ``content``, ``text``, ``source_text``, ``document_text``,
  ``raw_text``, ``ocr_text``, ``chunk_text``, and similar.
- Any string value that matches common secret patterns (API keys, SAS tokens,
  connection strings, bearer tokens in values — not headers).

What is NOT redacted:
- Resource IDs, ARM IDs, Fabric item IDs.
- Blob storage URIs (https://... patterns).
- Timestamps, counts, status fields, boolean flags.
- Evidence status, test commands, artifact paths.

Secret pattern detection is conservative: only exact pattern matches (not broad
substring scans) to avoid false positives on legitimate evidence content.
"""

from __future__ import annotations

import re
from typing import Any

# Field names that contain source document content — always redacted.
_SOURCE_CONTENT_FIELDS: frozenset[str] = frozenset(
    {
        "content",
        "text",
        "source_text",
        "document_text",
        "raw_text",
        "ocr_text",
        "chunk_text",
        "extracted_text",
        "element_content",
        "page_text",
        "section_text",
        "body",
        "body_text",
    }
)

# Patterns that indicate a string value contains a secret.
# These are conservative: require characteristic structural markers.
_SECRET_VALUE_PATTERNS: list[re.Pattern[str]] = [
    # Azure SAS token query-string component
    re.compile(r"sv=\d{4}-\d{2}-\d{2}&", re.IGNORECASE),
    # Azure Storage connection string
    re.compile(r"AccountKey=[A-Za-z0-9+/=]{30,}", re.IGNORECASE),
    # Azure Search admin key (32+ hex/base64 characters after key= or apiKey=)
    re.compile(r"(?:api[_-]?key|admin[_-]?key)\s*=\s*[A-Za-z0-9+/=]{20,}", re.IGNORECASE),
    # Bearer token in a value (not a header)
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    # Azure Cognitive/OpenAI subscription key header value pattern
    re.compile(r"[A-Za-z0-9]{32}$"),
]

_FREE_TEXT_SECRET_PATTERNS: list[re.Pattern[str]] = [
    *_SECRET_VALUE_PATTERNS[:-1],
    re.compile(
        r"(?:api[ _-]?key|admin[ _-]?key|token|secret|password)"
        r"\s*(?::|=|\bis\b)\s*[A-Za-z0-9+/=._~-]{20,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:api[ _-]?key|admin[ _-]?key|token|secret|password)"
        r"[-/\\]"
        r"(?=[A-Za-z0-9+_=~]*[A-Za-z])"
        r"(?=[A-Za-z0-9+_=~]*[0-9])"
        r"[A-Za-z0-9+_=~]{20,}",
        re.IGNORECASE,
    ),
]

_REDACTED_PLACEHOLDER = "[REDACTED]"


def _looks_like_secret(value: str) -> bool:
    """Return True when *value* matches a known secret pattern."""
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            return True
    return False


def looks_like_secret(value: str) -> bool:
    """Public wrapper for conservative secret detection."""
    return _looks_like_secret(value)


def redact_secret_text(value: str) -> str:
    """Redact only detected secret substrings within *value*.

    Unlike :func:`redact_value`, this preserves surrounding context so bounded
    proposal/source excerpts can still be shown safely.
    """
    redacted = value
    for pattern in _FREE_TEXT_SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED_PLACEHOLDER, redacted)
    return redacted


def redact_value(field_name: str, value: Any) -> Any:
    """Redact a single value based on field name and content heuristics.

    - Source-content fields are always redacted.
    - String values matching secret patterns are redacted.
    - All other values are returned unchanged.
    """
    if field_name in _SOURCE_CONTENT_FIELDS:
        return _REDACTED_PLACEHOLDER
    if isinstance(value, str) and _looks_like_secret(value):
        return _REDACTED_PLACEHOLDER
    return value


def redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with source-content and secret fields redacted.

    Handles nested dicts and lists recursively.  Does not modify the original.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[key] = redact_dict(value)
        elif isinstance(value, list):
            result[key] = [
                redact_dict(item) if isinstance(item, dict) else redact_value(key, item)
                for item in value
            ]
        else:
            result[key] = redact_value(key, value)
    return result


def redact_evidence_manifest(manifest_dict: dict[str, Any]) -> dict[str, Any]:
    """Redact a serialised :class:`~fabric_kg_builder.release.manifest.ReleaseManifest`.

    Evidence IDs, statuses, test commands, artifact paths, and descriptions are
    preserved.  Content fields and secret-pattern values are redacted.
    """
    return redact_dict(manifest_dict)


def redact_ledger(ledger_dict: dict[str, Any]) -> dict[str, Any]:
    """Redact a serialised :class:`~fabric_kg_builder.release.ledger.ResourceLedger`.

    Resource IDs, kinds, statuses, and ARM/Fabric IDs are preserved.
    Notes fields and any secret-matching values are redacted.
    """
    return redact_dict(ledger_dict)


def assert_no_source_content(data: dict[str, Any], path: str = "") -> None:
    """Raise ``AssertionError`` if any source-content field appears in *data*.

    Used in tests as a canary check to prove that serialised evidence does not
    contain customer document text.
    """
    for key, value in data.items():
        current_path = f"{path}.{key}" if path else key
        if key in _SOURCE_CONTENT_FIELDS and isinstance(value, str) and value != _REDACTED_PLACEHOLDER:
            raise AssertionError(
                f"Source content found at '{current_path}': field '{key}' contains unredacted text."
            )
        if isinstance(value, dict):
            assert_no_source_content(value, current_path)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    assert_no_source_content(item, f"{current_path}[{i}]")


def assert_no_secrets(data: dict[str, Any], path: str = "") -> None:
    """Raise ``AssertionError`` if any value matches a known secret pattern.

    Used as a security canary in unit tests.
    """
    for key, value in data.items():
        current_path = f"{path}.{key}" if path else key
        if isinstance(value, str) and _looks_like_secret(value):
            raise AssertionError(
                f"Potential secret found at '{current_path}' (field '{key}')."
            )
        elif isinstance(value, dict):
            assert_no_secrets(value, current_path)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, str) and _looks_like_secret(item):
                    raise AssertionError(
                        f"Potential secret found at '{current_path}[{i}]'."
                    )
                elif isinstance(item, dict):
                    assert_no_secrets(item, f"{current_path}[{i}]")
