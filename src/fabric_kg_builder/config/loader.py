"""Config loader: merges fabric-kg.yaml + .env + per-env JSON.

Resolution order (highest wins):
  1. CLI flag (caller passes overrides)
  2. Environment variable (from .env or shell)
  3. fabric-kg.yaml value (with ${ENV_VAR} interpolation)
  4. Per-env JSON (ontology/environments/{env}.json)
  5. Built-in default

Secrets (API keys, connection strings) are NEVER stored in yaml/json —
they live exclusively in .env and are loaded into os.environ via python-dotenv.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

from .schema import (
    AiSearchConfig,
    BlobStorageConfig,
    Config,
    DocumentIntelligenceConfig,
    FabricConfig,
    FoundryConfig,
)

# Matches ${VAR} and ${VAR:-default}
_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.*?))?\}")


# ---------------------------------------------------------------------------
# Interpolation helpers
# ---------------------------------------------------------------------------


def _interpolate(value: str) -> str:
    """Replace ``${VAR}`` / ``${VAR:-default}`` with environment variable values."""

    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2)  # None when no :- suffix
        val = os.environ.get(var_name)
        if val is not None:
            return val
        if default is not None:
            return default
        return match.group(0)  # leave unreplaced — caller decides whether to error

    return _ENV_VAR_RE.sub(_replace, value)


def _interpolate_deep(obj: object) -> object:
    """Recursively interpolate ${VAR} in all string values of a nested structure."""
    if isinstance(obj, dict):
        return {k: _interpolate_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_deep(item) for item in obj]
    if isinstance(obj, str):
        return _interpolate(obj)
    return obj


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(
    env: str = "dev",
    yaml_path: Optional[Path] = None,
    env_file: Optional[Path] = None,
    environments_dir: Optional[Path] = None,
) -> Config:
    """Load and merge config for *env* from all sources.

    Parameters
    ----------
    env:
        Target environment name — matches a file in ``ontology/environments/``.
    yaml_path:
        Override path to ``fabric-kg.yaml``.  Defaults to ``./fabric-kg.yaml``.
    env_file:
        Override path to ``.env``.  Defaults to ``./.env``.
    environments_dir:
        Override path to the environments directory.
        Defaults to ``./ontology/environments``.

    Returns
    -------
    Config
        Fully validated and merged configuration.

    Raises
    ------
    EnvironmentError
        When a required secret environment variable is missing (fail-fast).
    """
    root = Path.cwd()

    # 1. Load .env secrets into os.environ (override=False so shell wins)
    _env_file = env_file or root / ".env"
    if _env_file.exists():
        load_dotenv(_env_file, override=False)

    # 2. Load and interpolate fabric-kg.yaml
    _yaml_path = yaml_path or root / "fabric-kg.yaml"
    raw_yaml: dict = {}
    if _yaml_path.exists():
        with open(_yaml_path, "r", encoding="utf-8") as fh:
            raw_yaml = yaml.safe_load(fh) or {}
    raw_yaml = _interpolate_deep(raw_yaml)  # type: ignore[assignment]

    # 3. Load and interpolate per-env JSON
    _envs_dir = environments_dir or root / "ontology" / "environments"
    env_json_path = _envs_dir / f"{env}.json"
    env_cfg: dict = {}
    if env_json_path.exists():
        with open(env_json_path, "r", encoding="utf-8") as fh:
            env_cfg = json.load(fh)
        env_cfg = _interpolate_deep(env_cfg)  # type: ignore[assignment]

    # 4. Build typed config — env JSON wins over yaml for overlapping keys
    return _build_config(env, raw_yaml, env_cfg)


def load_fabric_ids(
    env: str = "dev",
    environments_dir: Optional[Path] = None,
) -> tuple[str, str]:
    """Return ``(workspace_id, lakehouse_item_id)`` from the per-env JSON.

    Does **not** require any secret environment variables — safe to call from
    compile and deploy commands without a fully configured .env.

    Returns empty strings when the env file or fabric section is absent.
    """
    root = Path.cwd()
    _envs_dir = environments_dir or root / "ontology" / "environments"
    env_json_path = _envs_dir / f"{env}.json"
    if env_json_path.exists():
        with open(env_json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        fabric = data.get("fabric", {})
        return (
            fabric.get("workspace_id", ""),
            fabric.get("lakehouse_item_id", ""),
        )
    return "", ""


def _resolved(value: object) -> str:
    """Return a usable string, or '' if the value is empty or an unresolved ${VAR}."""
    if not value or not isinstance(value, str):
        return ""
    return "" if _ENV_VAR_RE.search(value) else value


def _build_config(env: str, raw_yaml: dict, env_cfg: dict) -> Config:
    """Assemble a Config from the merged yaml + env-json dicts."""
    foundry_yaml = raw_yaml.get("foundry", {})
    enrichment_yaml = raw_yaml.get("enrichment", {})
    foundry_env = env_cfg.get("foundry", {})

    # Foundry endpoint is required — fail-fast with a clear message.
    # An unresolved ${VAR} placeholder (env var unset, no default) counts as missing.
    endpoint = (
        _resolved(foundry_env.get("endpoint"))
        or _resolved(foundry_yaml.get("endpoint"))
        or os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT", "")
    )
    if not endpoint:
        raise EnvironmentError(
            "Required secret 'AZURE_AI_FOUNDRY_ENDPOINT' is not set. "
            "Add it to your .env file (see .env.example for required keys)."
        )

    openai_endpoint = (
        _resolved(foundry_env.get("openai_endpoint"))
        or _resolved(foundry_yaml.get("openai_endpoint"))
        or os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    )

    foundry = FoundryConfig(
        endpoint=endpoint,
        openai_endpoint=openai_endpoint,
        project=foundry_env.get("project") or foundry_yaml.get("project", "example-project"),
        chat_deployment=(
            foundry_env.get("chat_deployment")
            or enrichment_yaml.get("chat_deployment", "gpt-5-4-mini")
        ),
        embedding_deployment=(
            foundry_env.get("embedding_deployment")
            or enrichment_yaml.get("embedding_deployment", "embedding")
        ),
        embedding_dimensions=int(
            foundry_env.get("embedding_dimensions")
            or enrichment_yaml.get("embedding_dimensions", 1536)
        ),
        api_version=(
            foundry_env.get("api_version")
            or foundry_yaml.get("api_version", "2024-12-01-preview")
        ),
    )

    fabric_env = env_cfg.get("fabric", {})
    fabric = FabricConfig(
        workspace_id=fabric_env.get("workspace_id", ""),
        lakehouse_item_id=fabric_env.get("lakehouse_item_id", ""),
        schema_name=fabric_env.get("schema_name", "dbo"),
    )

    blob_yaml = raw_yaml.get("blob_storage", {})
    blob_env = env_cfg.get("blob_storage", {})
    blob = BlobStorageConfig(
        account_name=blob_env.get("account_name") or blob_yaml.get("account_name", ""),
        container=blob_env.get("container") or blob_yaml.get("container", "kg-assets"),
        path_prefix=blob_env.get("path_prefix") or blob_yaml.get("path_prefix", ""),
    )

    search_yaml = raw_yaml.get("search", {})
    search_env = env_cfg.get("ai_search", {})
    ai_search = AiSearchConfig(
        enabled=search_env.get("enabled", search_yaml.get("enabled", False)),
        service_name=search_env.get("service_name") or search_yaml.get("service_name"),
        endpoint=search_env.get("endpoint") or search_yaml.get("endpoint", ""),
        index_prefix=search_env.get("index_prefix") or search_yaml.get("index_prefix", "kg-"),
    )

    docintel_yaml = raw_yaml.get("document_intelligence", {})
    docintel_env = env_cfg.get("document_intelligence", {})
    document_intelligence = DocumentIntelligenceConfig(
        endpoint=docintel_env.get("endpoint") or docintel_yaml.get("endpoint") or "",
    )

    return Config(
        env=env,
        foundry=foundry,
        fabric=fabric,
        blob=blob,
        ai_search=ai_search,
        document_intelligence=document_intelligence,
        auth_strategy=env_cfg.get("auth_strategy", "DefaultAzureCredential"),
    )
