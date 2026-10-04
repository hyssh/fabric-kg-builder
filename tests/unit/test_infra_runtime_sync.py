"""Tests for infra/runtime_sync.py — pure helpers and config sync."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_kg_builder.infra.runtime_sync import (
    _atomic_write_text,
    _load_json,
    _load_yaml,
    _resource_name,
    _set_if_present,
    sync_runtime_configuration,
)
from fabric_kg_builder.infra.schema import InfraManifest


# ---------------------------------------------------------------------------
# _set_if_present
# ---------------------------------------------------------------------------

class TestSetIfPresent:
    def test_sets_value_when_present(self):
        d = {}
        _set_if_present(d, "key", "value")
        assert d["key"] == "value"

    def test_skips_none(self):
        d = {}
        _set_if_present(d, "key", None)
        assert "key" not in d

    def test_skips_empty_string(self):
        d = {}
        _set_if_present(d, "key", "")
        assert "key" not in d

    def test_sets_zero(self):
        d = {}
        _set_if_present(d, "count", 0)
        assert d["count"] == 0

    def test_sets_false(self):
        d = {}
        _set_if_present(d, "flag", False)
        assert d["flag"] is False

    def test_sets_dict(self):
        d = {}
        _set_if_present(d, "nested", {"a": 1})
        assert d["nested"] == {"a": 1}


# ---------------------------------------------------------------------------
# _resource_name
# ---------------------------------------------------------------------------

class TestResourceName:
    def test_extracts_last_segment(self):
        arm_id = "/subscriptions/sub-001/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/mystorage"
        assert _resource_name(arm_id) == "mystorage"

    def test_simple_name(self):
        assert _resource_name("mystorage") == "mystorage"

    def test_none_returns_empty(self):
        assert _resource_name(None) == ""

    def test_trailing_slash(self):
        assert _resource_name("resource/") == "resource"

    def test_empty_string(self):
        assert _resource_name("") == ""


# ---------------------------------------------------------------------------
# _load_json
# ---------------------------------------------------------------------------

class TestLoadJson:
    def test_returns_empty_dict_if_missing(self, tmp_path):
        result = _load_json(tmp_path / "nonexistent.json")
        assert result == {}

    def test_loads_valid_json(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}')
        result = _load_json(f)
        assert result["key"] == "value"

    def test_raises_if_not_dict(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('[1, 2, 3]')
        with pytest.raises(ValueError, match="Expected an object"):
            _load_json(f)


# ---------------------------------------------------------------------------
# _load_yaml
# ---------------------------------------------------------------------------

class TestLoadYaml:
    def test_returns_empty_dict_if_missing(self, tmp_path):
        result = _load_yaml(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_loads_valid_yaml(self, tmp_path):
        f = tmp_path / "data.yaml"
        f.write_text("key: value\nnested:\n  a: 1\n")
        result = _load_yaml(f)
        assert result["key"] == "value"
        assert result["nested"]["a"] == 1

    def test_raises_if_not_dict(self, tmp_path):
        f = tmp_path / "data.yaml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="Expected a mapping"):
            _load_yaml(f)


# ---------------------------------------------------------------------------
# _atomic_write_text
# ---------------------------------------------------------------------------

class TestAtomicWriteText:
    def test_writes_file(self, tmp_path):
        target = tmp_path / "output.txt"
        _atomic_write_text(target, "hello world")
        assert target.read_text() == "hello world"

    def test_creates_parent_dir(self, tmp_path):
        target = tmp_path / "subdir" / "output.txt"
        _atomic_write_text(target, "content")
        assert target.exists()
        assert target.read_text() == "content"

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "output.txt"
        _atomic_write_text(target, "first")
        _atomic_write_text(target, "second")
        assert target.read_text() == "second"


# ---------------------------------------------------------------------------
# sync_runtime_configuration
# ---------------------------------------------------------------------------

def _make_minimal_manifest() -> InfraManifest:
    return InfraManifest.model_validate({
        "environment": "dev",
        "azure": {"subscription_id": "sub-001"},
    })


class TestSyncRuntimeConfiguration:
    def test_creates_fabric_config(self, tmp_path):
        fabric_env = tmp_path / "fabric_env.json"
        agent_metadata = tmp_path / "agent_metadata.yaml"
        manifest = _make_minimal_manifest()

        result = sync_runtime_configuration(
            environment="dev",
            manifest=manifest,
            outputs={
                "fabricWorkspaceId": "ws-001",
                "fabricLakehouseId": "lh-001",
            },
            fabric_environment_path=fabric_env,
            agent_metadata_path=agent_metadata,
        )
        assert isinstance(result, dict)
        # File should be written
        assert fabric_env.exists()
        data = json.loads(fabric_env.read_text())
        assert data["fabric"]["workspace_id"] == "ws-001"
        assert data["fabric"]["lakehouse_item_id"] == "lh-001"

    def test_search_defaults_set(self, tmp_path):
        fabric_env = tmp_path / "fabric_env.json"
        agent_metadata = tmp_path / "agent_metadata.yaml"
        manifest = _make_minimal_manifest()

        sync_runtime_configuration(
            environment="staging",
            manifest=manifest,
            outputs={"searchEndpoint": "https://search.example.com"},
            fabric_environment_path=fabric_env,
            agent_metadata_path=agent_metadata,
        )
        data = json.loads(fabric_env.read_text())
        assert data["ai_search"]["index_prefix"] == "kg-staging-"
        assert data["ai_search"]["endpoint"] == "https://search.example.com"

    def test_empty_outputs_still_works(self, tmp_path):
        fabric_env = tmp_path / "fabric_env.json"
        agent_metadata = tmp_path / "agent_metadata.yaml"
        manifest = _make_minimal_manifest()

        result = sync_runtime_configuration(
            environment="dev",
            manifest=manifest,
            outputs={},
            fabric_environment_path=fabric_env,
            agent_metadata_path=agent_metadata,
        )
        assert isinstance(result, dict)
        assert fabric_env.exists()
