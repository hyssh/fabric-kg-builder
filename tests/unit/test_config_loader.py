"""Tests for the config loader.

Verifies:
- ${VAR} and ${VAR:-default} interpolation in yaml string values
- Secrets (API keys) read from environment (via monkeypatch)
- Fail-fast EnvironmentError when a required secret env var is missing
- Per-env JSON values override yaml defaults
- Config object has correct types on all fields
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from fabric_kg_builder.config.loader import _interpolate, _interpolate_deep, load_config


# ---------------------------------------------------------------------------
# Interpolation unit tests
# ---------------------------------------------------------------------------


def test_interpolate_basic(monkeypatch):
    monkeypatch.setenv("MY_VAR", "hello")
    assert _interpolate("${MY_VAR}") == "hello"


def test_interpolate_default_used_when_not_set(monkeypatch):
    monkeypatch.delenv("UNSET_VAR", raising=False)
    assert _interpolate("${UNSET_VAR:-fallback}") == "fallback"


def test_interpolate_env_wins_over_default(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "from_env")
    assert _interpolate("${SOME_VAR:-fallback}") == "from_env"


def test_interpolate_unset_no_default_preserved():
    # When there is no default and the var is missing, leave the token as-is
    result = _interpolate("${TOTALLY_MISSING_NO_DEFAULT_XYZ}")
    assert result == "${TOTALLY_MISSING_NO_DEFAULT_XYZ}"


def test_interpolate_deep_nested(monkeypatch):
    monkeypatch.setenv("EP", "https://api.example.com")
    obj = {"a": {"b": "${EP}/path", "c": 42}, "d": ["${EP}", "literal"]}
    result = _interpolate_deep(obj)
    assert result["a"]["b"] == "https://api.example.com/path"
    assert result["a"]["c"] == 42  # non-string left alone
    assert result["d"][0] == "https://api.example.com"
    assert result["d"][1] == "literal"


# ---------------------------------------------------------------------------
# load_config — fixture-backed integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_project(tmp_path: Path, monkeypatch):
    """Creates a minimal project directory and changes into it."""
    monkeypatch.chdir(tmp_path)

    # Minimal fabric-kg.yaml with interpolation placeholder
    yaml_content = {
        "foundry": {
            "endpoint": "${AZURE_AI_FOUNDRY_ENDPOINT}",
            "project": "test-project",
        },
        "enrichment": {
            "chat_deployment": "gpt-5-4-mini",
            "embedding_deployment": "embedding",
            "embedding_dimensions": 1536,
        },
        "blob_storage": {
            "account_name": "myaccount",
            "container": "kg-assets",
        },
        "search": {"enabled": False},
        "document_intelligence": {"endpoint": "${AZURE_DOCINTEL_ENDPOINT:-}"},
    }
    (tmp_path / "fabric-kg.yaml").write_text(yaml.dump(yaml_content), encoding="utf-8")

    # Per-env JSON
    envs_dir = tmp_path / "ontology" / "environments"
    envs_dir.mkdir(parents=True)
    env_json = {
        "env": "dev",
        "auth_strategy": "DefaultAzureCredential",
        "fabric": {
            "workspace_id": "ws-1234",
            "lakehouse_item_id": "lh-5678",
        },
        "blob_storage": {
            "account_name": "examplestorageacct",
            "container": "kg-assets",
            "path_prefix": "dev/",
        },
        "ai_search": {
            "enabled": True,
            "service_name": "example-search",
            "index_prefix": "kg-dev-",
        },
        "foundry": {
            "project": "example-project",
            "chat_deployment": "gpt-5-4-mini",
            "embedding_deployment": "embedding",
            "embedding_dimensions": 1536,
        },
    }
    (envs_dir / "dev.json").write_text(json.dumps(env_json), encoding="utf-8")

    return tmp_path


def test_load_config_interpolates_endpoint(tmp_project, monkeypatch):
    """AZURE_AI_FOUNDRY_ENDPOINT from env is injected via ${...} interpolation."""
    monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://test.services.ai.azure.com")
    cfg = load_config(env="dev")
    assert cfg.foundry.endpoint == "https://test.services.ai.azure.com"


def test_load_config_reads_secret_from_env(tmp_project, monkeypatch):
    """Secret values are read from environment, never from yaml."""
    monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://secret.endpoint.example.com")
    cfg = load_config(env="dev")
    assert "secret.endpoint.example.com" in cfg.foundry.endpoint


def test_load_config_env_json_overrides_yaml(tmp_project, monkeypatch):
    """Per-env JSON workspace_id overrides yaml defaults."""
    monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://ep.example.com")
    cfg = load_config(env="dev")
    assert cfg.fabric.workspace_id == "ws-1234"
    assert cfg.fabric.lakehouse_item_id == "lh-5678"
    assert cfg.blob.path_prefix == "dev/"


def test_load_config_ai_search_enabled_from_env_json(tmp_project, monkeypatch):
    monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://ep.example.com")
    cfg = load_config(env="dev")
    assert cfg.ai_search.enabled is True
    assert cfg.ai_search.service_name == "example-search"
    assert cfg.ai_search.index_prefix == "kg-dev-"


def test_load_config_auth_strategy(tmp_project, monkeypatch):
    monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://ep.example.com")
    cfg = load_config(env="dev")
    assert cfg.auth_strategy == "DefaultAzureCredential"


def test_load_config_fails_fast_when_required_secret_missing(tmp_project, monkeypatch):
    """EnvironmentError raised immediately when AZURE_AI_FOUNDRY_ENDPOINT is absent."""
    monkeypatch.delenv("AZURE_AI_FOUNDRY_ENDPOINT", raising=False)

    # Rewrite yaml so the endpoint truly references an unset var (no :- default)
    yaml_content = {
        "foundry": {
            "endpoint": "${AZURE_AI_FOUNDRY_ENDPOINT}",  # no default
            "project": "test-project",
        },
    }
    (tmp_project / "fabric-kg.yaml").write_text(yaml.dump(yaml_content), encoding="utf-8")

    with pytest.raises(EnvironmentError, match="AZURE_AI_FOUNDRY_ENDPOINT"):
        load_config(env="dev")


def test_load_config_correct_types(tmp_project, monkeypatch):
    monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://ep.example.com")
    cfg = load_config(env="dev")
    assert isinstance(cfg.foundry.embedding_dimensions, int)
    assert cfg.foundry.embedding_dimensions == 1536
    assert isinstance(cfg.ai_search.enabled, bool)
