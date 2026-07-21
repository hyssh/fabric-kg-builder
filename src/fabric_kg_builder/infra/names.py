"""Deterministic Azure-safe resource name generation.

Names are derived from environment + suffix + short hash to ensure:
- Uniqueness within a subscription/region scope.
- Compliance with Azure resource naming rules (length, charset).
- Reproducibility — same inputs always produce the same name.

SPEC-006 §5.1 / INF-001: "Generated names are deterministic within Azure
naming rules and include a short hash to avoid collisions."
"""

from __future__ import annotations

import hashlib
import re
import unicodedata


# ---------------------------------------------------------------------------
# Naming constants per Azure resource type
# ---------------------------------------------------------------------------

_MAX_LENGTHS: dict[str, int] = {
    "storage": 24,           # Storage accounts: 3-24, lowercase alphanumeric only
    "document_intelligence": 64,  # Cognitive Services: up to 64
    "foundry": 64,            # CognitiveServices/accounts: up to 64
    "search": 60,             # Search services: 2-60
    "container_registry": 50, # Container registries: 5-50, alphanumeric only
    "identity": 128,          # Managed identity: up to 128
    "resource_group": 90,
    "monitoring": 260,        # Diagnostic settings: up to 260
}

_MIN_LENGTHS: dict[str, int] = {
    "storage": 3,
    "document_intelligence": 2,
    "foundry": 2,
    "search": 2,
    "container_registry": 5,
    "identity": 3,
    "resource_group": 1,
    "monitoring": 1,
}

# Storage accounts: only lowercase letters and digits, no hyphens.
_STORAGE_SAFE = re.compile(r"[^a-z0-9]")
# Most Azure resources: lowercase letters, digits, hyphens (no leading/trailing hyphen).
_GENERAL_SAFE = re.compile(r"[^a-z0-9-]")
_COLLAPSE_HYPHENS = re.compile(r"-{2,}")
_FABRIC_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_FABRIC_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1F\x7F]")


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _to_ascii_lower(text: str) -> str:
    """Normalize unicode, drop non-ASCII, lowercase."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", errors="ignore").decode("ascii")
    return ascii_only.lower()


def _short_hash(seed: str, length: int = 8) -> str:
    """Return a hex digest of *length* chars derived from *seed*."""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return digest[:length]


def _sanitize_general(text: str) -> str:
    """Sanitize a string for general Azure resource names."""
    clean = _to_ascii_lower(text)
    clean = _GENERAL_SAFE.sub("-", clean)
    clean = _COLLAPSE_HYPHENS.sub("-", clean)
    return clean.strip("-")


def _sanitize_storage(text: str) -> str:
    """Sanitize a string for storage account names (no hyphens)."""
    clean = _to_ascii_lower(text)
    return _STORAGE_SAFE.sub("", clean)


def _truncate_prefix(prefix: str, max_len: int, suffix: str) -> str:
    """Truncate prefix so that prefix + suffix fits within max_len."""
    available = max_len - len(suffix)
    if available <= 0:
        raise ValueError(
            f"Suffix '{suffix}' is too long ({len(suffix)}) for max_len={max_len}."
        )
    return prefix[:available]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def make_storage_name(environment: str, project: str = "kg") -> str:
    """Return a deterministic Azure Storage account name.

    Azure rules: 3–24 chars, lowercase alphanumeric only.

    Example: 'kgdevabcd1234' for environment='dev'.
    """
    seed = f"{project}-{environment}"
    hash_suffix = _short_hash(seed, 8)
    prefix = _sanitize_storage(f"{project}{environment}")
    truncated = _truncate_prefix(prefix, _MAX_LENGTHS["storage"], hash_suffix)
    name = truncated + hash_suffix
    if len(name) < _MIN_LENGTHS["storage"]:
        name = (project[:3] + name)[:_MAX_LENGTHS["storage"]]
    return name[:_MAX_LENGTHS["storage"]]


def make_document_intelligence_name(environment: str, project: str = "kg") -> str:
    """Return a deterministic Azure Document Intelligence resource name."""
    seed = f"{project}-docintel-{environment}"
    hash_suffix = _short_hash(seed, 6)
    prefix = _sanitize_general(f"{project}-docintel-{environment}")
    truncated = _truncate_prefix(prefix, _MAX_LENGTHS["document_intelligence"], f"-{hash_suffix}")
    name = f"{truncated}-{hash_suffix}"
    return name.strip("-")[: _MAX_LENGTHS["document_intelligence"]]


def make_foundry_name(environment: str, project: str = "kg") -> str:
    """Return a deterministic Azure AI Services (Foundry) account name."""
    seed = f"{project}-aiservices-{environment}"
    hash_suffix = _short_hash(seed, 6)
    prefix = _sanitize_general(f"{project}-aiservices-{environment}")
    truncated = _truncate_prefix(prefix, _MAX_LENGTHS["foundry"], f"-{hash_suffix}")
    name = f"{truncated}-{hash_suffix}"
    return name.strip("-")[: _MAX_LENGTHS["foundry"]]


def make_search_name(environment: str, project: str = "kg") -> str:
    """Return a deterministic Azure AI Search service name."""
    seed = f"{project}-search-{environment}"
    hash_suffix = _short_hash(seed, 6)
    prefix = _sanitize_general(f"{project}-search-{environment}")
    truncated = _truncate_prefix(prefix, _MAX_LENGTHS["search"], f"-{hash_suffix}")
    name = f"{truncated}-{hash_suffix}"
    return name.strip("-")[: _MAX_LENGTHS["search"]]


def make_container_registry_name(
    environment: str,
    project: str = "kg",
) -> str:
    """Return a deterministic lowercase-alphanumeric ACR name."""
    seed = f"{project}-acr-{environment}"
    hash_suffix = _short_hash(seed, 8)
    prefix = _sanitize_storage(f"{project}acr{environment}")
    truncated = _truncate_prefix(
        prefix,
        _MAX_LENGTHS["container_registry"],
        hash_suffix,
    )
    name = truncated + hash_suffix
    if len(name) < _MIN_LENGTHS["container_registry"]:
        name = f"kgacr{name}"
    return name[: _MAX_LENGTHS["container_registry"]]


def make_identity_name(environment: str, project: str = "kg") -> str:
    """Return a deterministic user-assigned managed identity name."""
    seed = f"{project}-id-{environment}"
    hash_suffix = _short_hash(seed, 6)
    prefix = _sanitize_general(f"{project}-id-{environment}")
    truncated = _truncate_prefix(prefix, _MAX_LENGTHS["identity"], f"-{hash_suffix}")
    name = f"{truncated}-{hash_suffix}"
    return name.strip("-")[: _MAX_LENGTHS["identity"]]


def make_monitoring_name(resource_name: str) -> str:
    """Return a deterministic diagnostic settings resource name."""
    seed = f"diag-{resource_name}"
    hash_suffix = _short_hash(seed, 6)
    prefix = _sanitize_general(f"diag-{resource_name}")
    truncated = _truncate_prefix(prefix, _MAX_LENGTHS["monitoring"], f"-{hash_suffix}")
    name = f"{truncated}-{hash_suffix}"
    return name.strip("-")[: _MAX_LENGTHS["monitoring"]]


def resolve_resource_name(
    configured_name: str | None,
    resource_type: str,
    environment: str,
    project: str = "kg",
) -> str:
    """Return the effective resource name: configured value or generated default.

    If a name is explicitly configured it is returned as-is (the user is
    responsible for compliance with Azure naming rules). Otherwise a
    deterministic name is generated.
    """
    if configured_name:
        return configured_name

    generators: dict[str, object] = {
        "storage": make_storage_name,
        "document_intelligence": make_document_intelligence_name,
        "foundry": make_foundry_name,
        "search": make_search_name,
        "container_registry": make_container_registry_name,
        "identity": make_identity_name,
    }
    generator = generators.get(resource_type)
    if generator is None:
        raise ValueError(f"No name generator for resource_type '{resource_type}'.")
    return generator(environment, project)  # type: ignore[operator]


def validate_fabric_identifier_name(name: str, item_type: str) -> str:
    """Validate Fabric names that require identifier-style display names.

    Lakehouse and Ontology creation reject spaces and hyphens. Validate these
    known service rules locally so an apply never creates a partial deployment
    before discovering an invalid item name.
    """
    if not _FABRIC_IDENTIFIER.fullmatch(name):
        raise ValueError(
            f"{item_type} name '{name}' must begin with a letter and contain "
            "only letters, numbers, and underscores."
        )
    return name


def validate_fabric_graph_model_name(name: str) -> str:
    """Validate the documented-safe Graph model display-name envelope."""
    if not name or not name.strip():
        raise ValueError("Graph model name must not be blank.")
    if len(name) > 256:
        raise ValueError("Graph model name must not exceed 256 characters.")
    if _FABRIC_CONTROL_CHARACTERS.search(name):
        raise ValueError("Graph model name must not contain control characters.")
    return name
