"""Unit tests for enrich_documents (Sprint 2 orchestrator extension).

Tests:
- enrich_documents assembles source_content from document_elements and calls enrich_batch.
- Produces canonical entities, relationships, evidence from LLM output.
- Works with mocked FoundryClient (no live API calls).
- section_path is included in source_content prefix when available.
- Resume/checkpoint is forwarded correctly.
- Works with no domain brief (no crash).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.enrichment.domain import DomainBrief
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.orchestrator import (
    CanonicalRecords,
    enrich_documents,
)
from fabric_kg_builder.model.schemas import DocumentElementRow

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_DUMMY_CONFIG = FoundryConfig(endpoint="https://test.endpoint/")
_SOURCE_FILE_ID = "src:enrich_docs_test_abc"
_NOW = datetime(2026, 6, 24, 14, 0, 0, tzinfo=timezone.utc)

_MOCK_LLM_OUTPUT = {
    "source_file_id": _SOURCE_FILE_ID,
    "pass": "p2",
    "entities": [
        {
            "id_hint": "device:surface-laptop-5",
            "type": "Device",
            "label": "Surface Laptop 5",
            "aliases": [],
            "confidence": 0.95,
        },
        {
            "id_hint": "component:battery",
            "type": "Component",
            "label": "Battery",
            "aliases": ["Battery pack"],
            "confidence": 0.90,
        },
    ],
    "relationships": [
        {
            "id_hint": "rel:has-battery",
            "source_id_hint": "device:surface-laptop-5",
            "relation": "has_component",
            "target_id_hint": "component:battery",
            "confidence": 0.88,
        }
    ],
    "chunks": [
        {
            "id_hint": "chunk:intro",
            "chunk_type": "section_text",
            "content": "Surface Laptop 5 has a replaceable battery.",
        }
    ],
    "visual_assets": [],
    "visual_regions": [],
    "evidence": [
        {
            "id_hint": "ev:1",
            "source_type": "document_span",
            "page_number": 1,
            "text": "The Surface Laptop 5 battery is replaceable.",
        }
    ],
    "placeholder_suggestions": [],
}

_DOMAIN_BRIEF = DomainBrief(
    domain_brief="Surface laptop hardware service documentation.",
    key_entity_types=["Device", "Component"],
    key_relationship_types=["has_component"],
    extraction_constraints=["Hardware only."],
    source_domain_text="Surface laptop service docs",
)


def _make_client(fixture_json: dict) -> FoundryClient:
    content_str = json.dumps(fixture_json)
    mock_sdk = MagicMock()
    completion = MagicMock(choices=[MagicMock(message=MagicMock(content=content_str))])
    mock_sdk.chat.completions.create.return_value = completion
    return FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)


def _make_elements() -> list[DocumentElementRow]:
    now = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
    return [
        DocumentElementRow(
            document_element_id="elem:001",
            source_file_id=_SOURCE_FILE_ID,
            element_type="section",
            title="Battery Replacement",
            content="Battery Replacement",
            section_path="Battery Replacement",
            page_number=1,
            sort_order=0,
            content_hash="hash001",
            extracted_at=now,
        ),
        DocumentElementRow(
            document_element_id="elem:002",
            source_file_id=_SOURCE_FILE_ID,
            element_type="paragraph",
            content="The Surface Laptop 5 battery is replaceable.",
            page_number=1,
            section_path="Battery Replacement",
            sort_order=1,
            content_hash="hash002",
            extracted_at=now,
        ),
        DocumentElementRow(
            document_element_id="elem:003",
            source_file_id=_SOURCE_FILE_ID,
            element_type="paragraph",
            content="Use a plastic spudger to lift the battery connector.",
            page_number=2,
            section_path="Battery Replacement",
            sort_order=2,
            content_hash="hash003",
            extracted_at=now,
        ),
    ]


# ---------------------------------------------------------------------------
# Tests: enrich_documents
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_documents_produces_entities(tmp_path: Path):
    client = _make_client(_MOCK_LLM_OUTPUT)
    records = enrich_documents(
        document_elements=_make_elements(),
        source_file_id=_SOURCE_FILE_ID,
        client=client,
        domain_brief=_DOMAIN_BRIEF,
        output_dir=tmp_path / "enriched",
    )
    assert len(records.entities) == 2
    entity_types = {e.entity_type for e in records.entities}
    assert "Device" in entity_types
    assert "Component" in entity_types


@pytest.mark.unit
def test_enrich_documents_produces_relationships(tmp_path: Path):
    client = _make_client(_MOCK_LLM_OUTPUT)
    records = enrich_documents(
        document_elements=_make_elements(),
        source_file_id=_SOURCE_FILE_ID,
        client=client,
        domain_brief=_DOMAIN_BRIEF,
        output_dir=tmp_path / "enriched",
    )
    assert len(records.relationships) == 1
    assert records.relationships[0].relationship_type == "has_component"


@pytest.mark.unit
def test_enrich_documents_produces_chunks(tmp_path: Path):
    client = _make_client(_MOCK_LLM_OUTPUT)
    records = enrich_documents(
        document_elements=_make_elements(),
        source_file_id=_SOURCE_FILE_ID,
        client=client,
        domain_brief=_DOMAIN_BRIEF,
        output_dir=tmp_path / "enriched",
    )
    assert len(records.chunks) == 1
    assert records.chunks[0].chunk_type == "section_text"


@pytest.mark.unit
def test_enrich_documents_produces_evidence(tmp_path: Path):
    client = _make_client(_MOCK_LLM_OUTPUT)
    records = enrich_documents(
        document_elements=_make_elements(),
        source_file_id=_SOURCE_FILE_ID,
        client=client,
        domain_brief=_DOMAIN_BRIEF,
        output_dir=tmp_path / "enriched",
    )
    assert len(records.evidence) == 1
    assert records.evidence[0].source_type == "document_span"


@pytest.mark.unit
def test_enrich_documents_includes_section_path_in_content(tmp_path: Path):
    """LLM user message should include section_path prefix for context."""
    captured_messages: list[str] = []

    mock_sdk = MagicMock()

    def capture_complete(**kwargs):
        msgs = kwargs.get("messages", [])
        for m in msgs:
            if m.get("role") == "user":
                captured_messages.append(m.get("content", ""))
        return MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps(_MOCK_LLM_OUTPUT)))])

    mock_sdk.chat.completions.create.side_effect = capture_complete
    client = FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)

    enrich_documents(
        document_elements=_make_elements(),
        source_file_id=_SOURCE_FILE_ID,
        client=client,
        domain_brief=None,
        output_dir=tmp_path / "enriched",
    )

    assert len(captured_messages) > 0
    combined = " ".join(captured_messages)
    assert "Battery Replacement" in combined


@pytest.mark.unit
def test_enrich_documents_skips_elements_without_content(tmp_path: Path):
    """Elements with None/empty content must not contribute blank lines."""
    now = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
    elements = [
        DocumentElementRow(
            document_element_id="elem:empty",
            source_file_id=_SOURCE_FILE_ID,
            element_type="page",
            content=None,  # no content
            page_number=1,
            sort_order=0,
            content_hash="hx",
            extracted_at=now,
        ),
        DocumentElementRow(
            document_element_id="elem:real",
            source_file_id=_SOURCE_FILE_ID,
            element_type="paragraph",
            content="Surface Laptop 5 battery.",
            page_number=1,
            sort_order=1,
            content_hash="hy",
            extracted_at=now,
        ),
    ]

    captured: list[str] = []

    mock_sdk = MagicMock()

    def capture(**kwargs):
        for m in kwargs.get("messages", []):
            if m.get("role") == "user":
                captured.append(m["content"])
        return MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps(_MOCK_LLM_OUTPUT)))])

    mock_sdk.chat.completions.create.side_effect = capture
    client = FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)

    enrich_documents(
        document_elements=elements,
        source_file_id=_SOURCE_FILE_ID,
        client=client,
        domain_brief=None,
        output_dir=tmp_path / "enriched",
    )

    combined = " ".join(captured)
    # The empty element's None content must not produce a "[page] None" line
    assert "[page] None" not in combined
    assert "Surface Laptop 5 battery" in combined


@pytest.mark.unit
def test_enrich_documents_no_domain_brief(tmp_path: Path):
    """enrich_documents must work when domain_brief is None."""
    client = _make_client(_MOCK_LLM_OUTPUT)
    records = enrich_documents(
        document_elements=_make_elements(),
        source_file_id=_SOURCE_FILE_ID,
        client=client,
        domain_brief=None,
        output_dir=tmp_path / "enriched",
    )
    assert isinstance(records, CanonicalRecords)
    assert len(records.entities) >= 1


@pytest.mark.unit
def test_enrich_documents_writes_checkpoint(tmp_path: Path):
    client = _make_client(_MOCK_LLM_OUTPUT)
    out_dir = tmp_path / "enriched"
    enrich_documents(
        document_elements=_make_elements(),
        source_file_id=_SOURCE_FILE_ID,
        client=client,
        domain_brief=None,
        output_dir=out_dir,
    )
    checkpoint = out_dir / ".checkpoint.json"
    assert checkpoint.exists()
    data = json.loads(checkpoint.read_text())
    assert _SOURCE_FILE_ID in data["completed"]


@pytest.mark.unit
def test_enrich_documents_resume_skips_completed(tmp_path: Path):
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)
    checkpoint = out_dir / ".checkpoint.json"
    checkpoint.write_text(json.dumps({"completed": [_SOURCE_FILE_ID]}), encoding="utf-8")

    mock_sdk = MagicMock()
    mock_sdk.chat.completions.create.side_effect = AssertionError(
        "LLM must not be called for completed source_file_id"
    )
    client = FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)

    result = enrich_documents(
        document_elements=_make_elements(),
        source_file_id=_SOURCE_FILE_ID,
        client=client,
        domain_brief=None,
        output_dir=out_dir,
        resume=True,
    )
    assert result.entities == []
