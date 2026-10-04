"""Configuration loading and validation for fabric-kg-builder.

Handles fabric-kg.yaml (non-secret config) and .env (secrets) with
precedence: CLI flag > env var > yaml > built-in default.
"""

from .loader import load_config, load_enrichment_config, resolve_max_concurrent
from .schema import (
    AiSearchConfig,
    BlobStorageConfig,
    Config,
    DocumentIntelligenceConfig,
    EnrichmentConfig,
    FabricConfig,
    FoundryConfig,
)

__all__ = [
    "load_config",
    "load_enrichment_config",
    "resolve_max_concurrent",
    "Config",
    "EnrichmentConfig",
    "FoundryConfig",
    "FabricConfig",
    "BlobStorageConfig",
    "AiSearchConfig",
    "DocumentIntelligenceConfig",
]
