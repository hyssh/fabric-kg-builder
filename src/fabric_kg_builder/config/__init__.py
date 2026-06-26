"""Configuration loading and validation for fabric-kg-builder.

Handles fabric-kg.yaml (non-secret config) and .env (secrets) with
precedence: CLI flag > env var > yaml > built-in default.
"""

from .loader import load_config
from .schema import (
    AiSearchConfig,
    BlobStorageConfig,
    Config,
    DocumentIntelligenceConfig,
    FabricConfig,
    FoundryConfig,
)

__all__ = [
    "load_config",
    "Config",
    "FoundryConfig",
    "FabricConfig",
    "BlobStorageConfig",
    "AiSearchConfig",
    "DocumentIntelligenceConfig",
]
