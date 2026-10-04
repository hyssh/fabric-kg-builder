"""Unit tests for the enrich and set-domain CLI commands.

All tests use mock LLM clients — no live API calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from tests.conftest import write_approved_domain_contract


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DUMMY_CONFIG = FoundryConfig(endpoint="https://test.endpoint/")

_MOCK_DOMAIN_BRIEF = {
    "domain_brief": "Surface laptop hardware service documentation.",
    "key_entity_types": ["Device", "Component", "PartNumber"],
    "key_relationship_types": ["has_component"],
    "extraction_constraints": ["Hardware only."],
    "source_domain_text": "Surface laptop service docs",
}

_MOCK_LLM_OUTPUT = {
    "source_file_id": "src:test",
    "pass": "p2",
    "entities": [
        {
            "id_hint": "device:surface-laptop-5",
            "type": "Device",
            "label": "Surface Laptop 5",
            "aliases": [],
            "confidence": 0.95,
        },
    ],
    "relationships": [],
    "chunks": [],
    "visual_assets": [],
    "visual_regions": [],
    "evidence": [],
    "placeholder_suggestions": [],
}


def _make_client(fixture_json: dict) -> FoundryClient:
    """Build a FoundryClient with a mock SDK returning *fixture_json*."""
    content_str = json.dumps(fixture_json)
    mock_sdk = MagicMock()
    completion = MagicMock(choices=[MagicMock(message=MagicMock(content=content_str))])
    mock_sdk.chat.completions.create.return_value = completion
    return FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)


# ---------------------------------------------------------------------------
# set-domain tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetDomainCmd:
    def test_writes_domain_json(self, tmp_path: Path):
        """set-domain --prompt writes build/enriched/domain.json."""
        out_dir = tmp_path / "enriched"
        mock_client = _make_client(_MOCK_DOMAIN_BRIEF)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["set-domain", "--prompt", "Surface laptop service docs", "--out", str(out_dir),
             "--industry", "manufacturing", "--business-domain", "field-service"],
            obj={"_foundry_client": mock_client},
        )
        assert result.exit_code == 0, f"set-domain failed: {result.output}\n{result.exception}"
        domain_file = out_dir / "domain.json"
        assert domain_file.exists(), "domain.json was not written"
        data = json.loads(domain_file.read_text())
        assert "domain_brief" in data

    def test_skips_if_exists_without_force(self, tmp_path: Path):
        """set-domain skips rephrase if domain.json already exists (no --force)."""
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)
        existing = out_dir / "domain.json"
        existing.write_text(json.dumps(_MOCK_DOMAIN_BRIEF), encoding="utf-8")

        mock_client = _make_client(_MOCK_DOMAIN_BRIEF)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["set-domain", "--prompt", "new text", "--out", str(out_dir),
             "--industry", "manufacturing", "--business-domain", "field-service"],
            obj={"_foundry_client": mock_client},
        )
        assert result.exit_code == 0
        # LLM should NOT have been called (file already exists, no --force)
        mock_client._client.chat.completions.create.assert_not_called()

    def test_force_overwrites_existing(self, tmp_path: Path):
        """set-domain --force reruns rephrase even if domain.json exists."""
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)
        existing = out_dir / "domain.json"
        existing.write_text(json.dumps(_MOCK_DOMAIN_BRIEF), encoding="utf-8")

        mock_client = _make_client(_MOCK_DOMAIN_BRIEF)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["set-domain", "--prompt", "new text", "--out", str(out_dir), "--force",
             "--industry", "manufacturing", "--business-domain", "field-service"],
            obj={"_foundry_client": mock_client},
        )
        assert result.exit_code == 0
        mock_client._client.chat.completions.create.assert_called_once()

    def test_error_without_prompt_or_file(self, tmp_path: Path):
        """set-domain must fail if neither --prompt nor --domain-file is given."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["set-domain", "--out", str(tmp_path),
             "--industry", "manufacturing", "--business-domain", "field-service"],
            obj={"_foundry_client": _make_client(_MOCK_DOMAIN_BRIEF)},
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# enrich command tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnrichCmd:
    def test_exits_0_with_mock(self, tmp_path: Path, sample_csv_path: Path):
        """enrich --input <csv> exits 0 with a mock LLM client."""
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)

        # Enrichment now requires an approved domain.yaml contract.
        write_approved_domain_contract(out_dir / "domain.yaml")

        mock_client = _make_client(_MOCK_LLM_OUTPUT)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "enrich",
                "--input", str(sample_csv_path),
                "--out", str(out_dir),
            ],
            obj={"_foundry_client": mock_client},
        )
        assert result.exit_code == 0, (
            f"enrich exited {result.exit_code}.\nOutput: {result.output}\n"
            f"Exception: {result.exception}"
        )

    def test_exits_0_with_domain_prompt(self, tmp_path: Path, sample_csv_path: Path):
        """enrich with an approved domain.yaml and mock client exits 0."""
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)

        # Enrichment requires an approved domain.yaml; --domain-prompt is deprecated.
        write_approved_domain_contract(out_dir / "domain.yaml")

        mock_client = _make_client(_MOCK_LLM_OUTPUT)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "enrich",
                "--input", str(sample_csv_path),
                "--out", str(out_dir),
            ],
            obj={"_foundry_client": mock_client},
        )
        assert result.exit_code == 0, (
            f"enrich exited {result.exit_code}.\nOutput: {result.output}\n"
            f"Exception: {result.exception}"
        )

    def test_writes_output_json(self, tmp_path: Path, sample_csv_path: Path):
        """enrich writes at least one output JSON file to --out directory."""
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)
        write_approved_domain_contract(out_dir / "domain.yaml")

        mock_client = _make_client(_MOCK_LLM_OUTPUT)
        runner = CliRunner()
        runner.invoke(
            cli,
            ["enrich", "--input", str(sample_csv_path), "--out", str(out_dir)],
            obj={"_foundry_client": mock_client},
        )
        output_files = [f for f in out_dir.glob("*.json") if f.name not in ("domain.yaml", ".checkpoint.json")]
        assert len(output_files) >= 1, "No output JSON files written"
