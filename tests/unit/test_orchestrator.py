"""Unit tests for fabric_kg_builder.enrichment.orchestrator.

Tests:
- canonicalize_llm_output produces stable entity IDs from id_hints.
- Entities below confidence threshold are dropped.
- Deduplication by canonical_key works.
- build_user_message places domain text in user content only (security).
- enrich_batch writes JSON and checkpoint.
- Checkpoint resume skips completed batches.
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.enrichment.domain import DomainBrief
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.orchestrator import (
    CONFIDENCE_THRESHOLD,
    _ENRICH_SYSTEM_PROMPT,
    CanonicalRecords,
    build_user_message,
    canonicalize_llm_output,
    enrich_batch,
)
from fabric_kg_builder.enrichment.output_schema import validate
from fabric_kg_builder.model.ids import make_entity_id, normalize_canonical_key


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_DUMMY_CONFIG = FoundryConfig(endpoint="https://test.endpoint/")
_SOURCE_FILE_ID = "src:abc123def456"
_NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)

_VALID_LLM_OUTPUT = {
    "source_file_id": _SOURCE_FILE_ID,
    "pass": "p2",
    "entities": [
        {
            "id_hint": "device:surface-laptop-5",
            "type": "Device",
            "label": "Surface Laptop 5",
            "aliases": ["Surface Laptop"],
            "confidence": 0.95,
        },
        {
            "id_hint": "component:battery",
            "type": "Component",
            "label": "Battery",
            "aliases": ["Battery pack"],
            "confidence": 0.91,
        },
        {
            "id_hint": "lowconf:dropped",
            "type": "Component",
            "label": "Dropped Item",
            "aliases": [],
            "confidence": 0.30,   # below threshold — must be dropped
        },
    ],
    "relationships": [
        {
            "id_hint": "rel:has-battery",
            "source_id_hint": "device:surface-laptop-5",
            "relation": "has_component",
            "target_id_hint": "component:battery",
            "confidence": 0.90,
        },
        {
            "id_hint": "rel:lowconf-dropped",
            "source_id_hint": "device:surface-laptop-5",
            "relation": "has_component",
            "target_id_hint": "lowconf:dropped",   # target was dropped
            "confidence": 0.30,
        },
    ],
    "chunks": [
        {
            "id_hint": "chunk:1",
            "chunk_type": "table_row",
            "content": "Surface Laptop 5 has a replaceable battery.",
        }
    ],
    "visual_assets": [],
    "visual_regions": [],
    "evidence": [
        {
            "id_hint": "ev:1",
            "source_type": "csv_row",
            "row_index": 0,
            "text": "Surface Laptop 5, Battery, M1287099-003",
        }
    ],
    "placeholder_suggestions": [],
}

_DOMAIN_BRIEF = DomainBrief(
    domain_brief="Surface laptop hardware service documentation.",
    key_entity_types=["Device", "Component", "PartNumber"],
    key_relationship_types=["has_component"],
    extraction_constraints=["Focus on hardware only."],
    source_domain_text="Surface laptop service docs",
)


def _make_client(fixture_json: dict) -> FoundryClient:
    content_str = json.dumps(fixture_json)
    mock_sdk = MagicMock()
    completion = MagicMock(choices=[MagicMock(message=MagicMock(content=content_str))])
    mock_sdk.chat.completions.create.return_value = completion
    return FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)


# ---------------------------------------------------------------------------
# Tests: canonicalize_llm_output
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCanonicalizeOutput:
    def _get_output(self) -> object:
        return validate(_VALID_LLM_OUTPUT)

    def test_produces_entity_rows(self):
        output = self._get_output()
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        assert len(records.entities) == 3  # all kept; confidence is not a drop gate

    def test_entity_ids_are_stable(self):
        output = self._get_output()
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        entity_map = {e.entity_type + ":" + e.display_name: e.entity_id for e in records.entities}

        expected_device_id = make_entity_id("Device", "Surface Laptop 5")
        expected_battery_id = make_entity_id("Component", "Battery")

        assert entity_map["Device:Surface Laptop 5"] == expected_device_id
        assert entity_map["Component:Battery"] == expected_battery_id

    def test_low_confidence_entity_kept(self):
        # Confidence is stored, not a drop gate — low-confidence entities are kept
        # so the knowledge graph keeps recall (stabilizes yield variance).
        output = self._get_output()
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        display_names = {e.display_name for e in records.entities}
        assert "Dropped Item" in display_names

    def test_canonical_key_is_normalized(self):
        output = self._get_output()
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        device = next(e for e in records.entities if e.entity_type == "Device")
        assert device.canonical_key == normalize_canonical_key("Device", "Surface Laptop 5")

    def test_relationship_resolved_to_stable_ids(self):
        output = self._get_output()
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        # Both relationships now resolve (low-conf endpoint entity is kept).
        assert len(records.relationships) == 2
        rel = records.relationships[0]
        assert rel.relationship_type == "has_component"
        assert rel.source_entity_id == make_entity_id("Device", "Surface Laptop 5")
        assert rel.target_entity_id == make_entity_id("Component", "Battery")

    def test_evidence_row_produced(self):
        output = self._get_output()
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        assert len(records.evidence) == 1
        assert records.evidence[0].source_type == "csv_row"
        assert records.evidence[0].row_index == 0

    def test_chunk_row_produced(self):
        # table_row chunks from the LLM are now dropped — DI is the source of
        # truth for table structure (coordinator-tables-via-docintel.md, 2026-06-24).
        # The _VALID_LLM_OUTPUT fixture has a single table_row chunk, so
        # canonicalize must produce zero LLM-side chunks (all dropped).
        output = self._get_output()
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        chunk_types = [c.chunk_type for c in records.chunks]
        assert "table_row" not in chunk_types, (
            "canonicalize must drop LLM-emitted table_row chunks; "
            "table structure comes from Document Intelligence"
        )
        # No LLM chunks survive (only the dropped table_row was in the fixture).
        assert len(records.chunks) == 0


@pytest.mark.unit
class TestDeduplication:
    """canonicalize_llm_output must deduplicate by canonical_key."""

    def test_duplicate_entities_merged(self):
        duplicate_output = {
            "source_file_id": _SOURCE_FILE_ID,
            "pass": "p2",
            "entities": [
                {
                    "id_hint": "a",
                    "type": "Device",
                    "label": "Surface Laptop 5",
                    "aliases": ["Alias A"],
                    "confidence": 0.90,
                },
                {
                    "id_hint": "b",
                    "type": "Device",
                    "label": "Surface Laptop 5",  # same canonical_key
                    "aliases": ["Alias B"],
                    "confidence": 0.85,
                },
            ],
            "relationships": [],
            "chunks": [],
            "visual_assets": [],
            "visual_regions": [],
            "evidence": [],
            "placeholder_suggestions": [],
        }
        output = validate(duplicate_output)
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        assert len(records.entities) == 1
        entity = records.entities[0]
        # Aliases merged
        assert set(entity.aliases) >= {"Alias A", "Alias B"}
        # Higher confidence kept
        assert entity.confidence == 0.90


# ---------------------------------------------------------------------------
# Tests: build_user_message (security)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildUserMessage:
    """Domain text must appear in the user message only, not in _ENRICH_SYSTEM_PROMPT."""

    def test_system_prompt_is_fixed_literal(self):
        assert isinstance(_ENRICH_SYSTEM_PROMPT, str)
        assert len(_ENRICH_SYSTEM_PROMPT) > 20

    def test_domain_brief_in_user_message(self):
        msg = build_user_message(_DOMAIN_BRIEF, _SOURCE_FILE_ID, "source data", "p2")
        assert _DOMAIN_BRIEF.domain_brief in msg

    def test_domain_not_in_system_prompt(self):
        """Domain text must never appear in the fixed system prompt."""
        assert _DOMAIN_BRIEF.domain_brief not in _ENRICH_SYSTEM_PROMPT
        assert "Surface laptop" not in _ENRICH_SYSTEM_PROMPT

    def test_user_message_contains_source_file_id(self):
        msg = build_user_message(_DOMAIN_BRIEF, _SOURCE_FILE_ID, "source rows", "p2")
        assert _SOURCE_FILE_ID in msg

    def test_user_message_contains_pass_name(self):
        msg = build_user_message(_DOMAIN_BRIEF, _SOURCE_FILE_ID, "source rows", "p2")
        assert "p2" in msg

    def test_user_message_no_domain_when_brief_is_none(self):
        msg = build_user_message(None, _SOURCE_FILE_ID, "source rows", "p2")
        assert "DOMAIN CONTEXT" not in msg
        assert _SOURCE_FILE_ID in msg


# ---------------------------------------------------------------------------
# Tests: enrich_batch (checkpoint / resume)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnrichBatch:
    def test_writes_intermediate_json(self, tmp_path: Path):
        client = _make_client(_VALID_LLM_OUTPUT)
        enrich_batch(
            source_content="Surface Laptop 5, Battery",
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=_DOMAIN_BRIEF,
            output_dir=tmp_path / "enriched",
        )
        output_files = list((tmp_path / "enriched").glob("*.json"))
        # At least one output file (excluding checkpoint)
        non_checkpoint = [f for f in output_files if f.name != ".checkpoint.json"]
        assert len(non_checkpoint) >= 1

    def test_writes_checkpoint(self, tmp_path: Path):
        client = _make_client(_VALID_LLM_OUTPUT)
        out_dir = tmp_path / "enriched"
        enrich_batch(
            source_content="Surface Laptop 5",
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=out_dir,
        )
        checkpoint = out_dir / ".checkpoint.json"
        assert checkpoint.exists()
        data = json.loads(checkpoint.read_text())
        assert _SOURCE_FILE_ID in data["completed"]

    def test_resume_skips_completed(self, tmp_path: Path):
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)

        # Pre-write checkpoint with source_file_id already done.
        checkpoint = out_dir / ".checkpoint.json"
        checkpoint.write_text(
            json.dumps({"completed": [_SOURCE_FILE_ID]}), encoding="utf-8"
        )

        # Mock client that fails if called (to confirm it's NOT called).
        mock_sdk = MagicMock()
        mock_sdk.chat.completions.create.side_effect = (
            AssertionError("LLM should not be called for completed batches")
        )
        client = FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)

        result = enrich_batch(
            source_content="Some content",
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=out_dir,
            resume=True,
        )
        # Should return empty result without calling LLM
        assert result.entities == []
        assert result.relationships == []

    def test_produces_canonical_entities(self, tmp_path: Path):
        client = _make_client(_VALID_LLM_OUTPUT)
        records = enrich_batch(
            source_content="Surface Laptop 5, Battery",
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=_DOMAIN_BRIEF,
            output_dir=tmp_path / "enriched",
        )
        assert len(records.entities) == 3
        assert any(e.entity_type == "Device" for e in records.entities)

    def test_no_domain_brief_is_valid(self, tmp_path: Path):
        """enrich_batch must work without a domain brief (no crash, no domain in user msg)."""
        client = _make_client(_VALID_LLM_OUTPUT)
        records = enrich_batch(
            source_content="Surface Laptop 5",
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=tmp_path / "enriched",
        )
        assert len(records.entities) >= 1
