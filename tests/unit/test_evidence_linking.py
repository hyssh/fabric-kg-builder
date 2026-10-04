"""Unit tests for text and visual evidence linking (Sprint 2).

Tests:
- link_text_evidence with chunk_id sets source_type="chunk".
- link_text_evidence without chunk_id sets source_type="document_span".
- link_text_evidence sets correct FKs (document_element_id, chunk_id).
- link_visual_evidence with callout_id sets source_type="figure_callout".
- link_visual_evidence without callout_id sets source_type="image_region".
- link_visual_evidence sets image_id, visual_region_id, blob_url.
- Evidence IDs are stable (deterministic).
- created_at is injectable for tests.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fabric_kg_builder.enrichment.orchestrator import link_text_evidence, link_visual_evidence
from fabric_kg_builder.model.ids import make_evidence_id, content_hash
from fabric_kg_builder.model.schemas import EvidenceRow

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_FILE_ID = "src:evidence_test_abc"
_NOW = datetime(2026, 6, 24, 15, 0, 0, tzinfo=timezone.utc)
_CHUNK_ID = "chunk:test_chunk_001"
_ELEMENT_ID = "elem:test_element_001"
_IMAGE_ID = "img:test_image_001"
_REGION_ID = "vr:test_region_001"
_CALLOUT_ID = "vr:test_callout_001"
_BLOB_URL = "https://fake.blob.core.windows.net/kg-assets/img001.png"


# ---------------------------------------------------------------------------
# Tests: link_text_evidence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_link_text_evidence_chunk_source_type():
    ev = link_text_evidence(
        _SOURCE_FILE_ID,
        chunk_id=_CHUNK_ID,
        document_element_id=_ELEMENT_ID,
        text="The battery is replaceable.",
        now=_NOW,
    )
    assert ev.source_type == "chunk"


@pytest.mark.unit
def test_link_text_evidence_document_span_when_no_chunk_id():
    ev = link_text_evidence(
        _SOURCE_FILE_ID,
        document_element_id=_ELEMENT_ID,
        text="Use a plastic spudger.",
        now=_NOW,
    )
    assert ev.source_type == "document_span"


@pytest.mark.unit
def test_link_text_evidence_sets_chunk_id_fk():
    ev = link_text_evidence(
        _SOURCE_FILE_ID,
        chunk_id=_CHUNK_ID,
        now=_NOW,
    )
    assert ev.chunk_id == _CHUNK_ID


@pytest.mark.unit
def test_link_text_evidence_sets_document_element_id_fk():
    ev = link_text_evidence(
        _SOURCE_FILE_ID,
        document_element_id=_ELEMENT_ID,
        now=_NOW,
    )
    assert ev.document_element_id == _ELEMENT_ID


@pytest.mark.unit
def test_link_text_evidence_sets_source_file_id():
    ev = link_text_evidence(_SOURCE_FILE_ID, now=_NOW)
    assert ev.source_file_id == _SOURCE_FILE_ID


@pytest.mark.unit
def test_link_text_evidence_sets_page_number():
    ev = link_text_evidence(_SOURCE_FILE_ID, page_number=3, now=_NOW)
    assert ev.page_number == 3


@pytest.mark.unit
def test_link_text_evidence_sets_section_path():
    ev = link_text_evidence(
        _SOURCE_FILE_ID,
        section_path="Battery Replacement/Procedure",
        now=_NOW,
    )
    assert ev.section_path == "Battery Replacement/Procedure"


@pytest.mark.unit
def test_link_text_evidence_sets_text():
    ev = link_text_evidence(
        _SOURCE_FILE_ID,
        text="Surface Laptop 5 battery capacity is 45.8Wh.",
        now=_NOW,
    )
    assert ev.text == "Surface Laptop 5 battery capacity is 45.8Wh."


@pytest.mark.unit
def test_link_text_evidence_sets_created_at():
    ev = link_text_evidence(_SOURCE_FILE_ID, now=_NOW)
    assert ev.created_at == _NOW


@pytest.mark.unit
def test_link_text_evidence_sets_content_hash():
    text = "some evidence text"
    ev = link_text_evidence(_SOURCE_FILE_ID, text=text, now=_NOW)
    assert ev.content_hash == content_hash(text)


@pytest.mark.unit
def test_link_text_evidence_id_is_stable():
    """Same inputs must always produce the same evidence_id."""
    ev1 = link_text_evidence(
        _SOURCE_FILE_ID,
        chunk_id=_CHUNK_ID,
        document_element_id=_ELEMENT_ID,
        text="stable text",
        page_number=1,
        now=_NOW,
    )
    ev2 = link_text_evidence(
        _SOURCE_FILE_ID,
        chunk_id=_CHUNK_ID,
        document_element_id=_ELEMENT_ID,
        text="stable text",
        page_number=1,
        now=_NOW,
    )
    assert ev1.evidence_id == ev2.evidence_id


@pytest.mark.unit
def test_link_text_evidence_different_text_different_id():
    ev1 = link_text_evidence(_SOURCE_FILE_ID, text="text A", now=_NOW)
    ev2 = link_text_evidence(_SOURCE_FILE_ID, text="text B", now=_NOW)
    assert ev1.evidence_id != ev2.evidence_id


@pytest.mark.unit
def test_link_text_evidence_returns_evidence_row():
    ev = link_text_evidence(_SOURCE_FILE_ID, now=_NOW)
    assert isinstance(ev, EvidenceRow)


# ---------------------------------------------------------------------------
# Tests: link_visual_evidence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_link_visual_evidence_image_region_source_type():
    ev = link_visual_evidence(
        _SOURCE_FILE_ID,
        _IMAGE_ID,
        visual_region_id=_REGION_ID,
        now=_NOW,
    )
    assert ev.source_type == "image_region"


@pytest.mark.unit
def test_link_visual_evidence_figure_callout_source_type():
    ev = link_visual_evidence(
        _SOURCE_FILE_ID,
        _IMAGE_ID,
        callout_id=_CALLOUT_ID,
        now=_NOW,
    )
    assert ev.source_type == "figure_callout"


@pytest.mark.unit
def test_link_visual_evidence_sets_image_id_fk():
    ev = link_visual_evidence(_SOURCE_FILE_ID, _IMAGE_ID, now=_NOW)
    assert ev.image_id == _IMAGE_ID


@pytest.mark.unit
def test_link_visual_evidence_sets_visual_region_id_fk():
    ev = link_visual_evidence(
        _SOURCE_FILE_ID,
        _IMAGE_ID,
        visual_region_id=_REGION_ID,
        now=_NOW,
    )
    assert ev.visual_region_id == _REGION_ID


@pytest.mark.unit
def test_link_visual_evidence_sets_callout_id():
    ev = link_visual_evidence(
        _SOURCE_FILE_ID,
        _IMAGE_ID,
        callout_id=_CALLOUT_ID,
        now=_NOW,
    )
    assert ev.callout_id == _CALLOUT_ID


@pytest.mark.unit
def test_link_visual_evidence_sets_blob_url():
    ev = link_visual_evidence(
        _SOURCE_FILE_ID,
        _IMAGE_ID,
        blob_url=_BLOB_URL,
        now=_NOW,
    )
    assert ev.blob_url == _BLOB_URL


@pytest.mark.unit
def test_link_visual_evidence_sets_page_number():
    ev = link_visual_evidence(_SOURCE_FILE_ID, _IMAGE_ID, page_number=5, now=_NOW)
    assert ev.page_number == 5


@pytest.mark.unit
def test_link_visual_evidence_sets_text():
    ev = link_visual_evidence(
        _SOURCE_FILE_ID,
        _IMAGE_ID,
        text="Battery connector callout B",
        now=_NOW,
    )
    assert ev.text == "Battery connector callout B"


@pytest.mark.unit
def test_link_visual_evidence_sets_source_file_id():
    ev = link_visual_evidence(_SOURCE_FILE_ID, _IMAGE_ID, now=_NOW)
    assert ev.source_file_id == _SOURCE_FILE_ID


@pytest.mark.unit
def test_link_visual_evidence_sets_created_at():
    ev = link_visual_evidence(_SOURCE_FILE_ID, _IMAGE_ID, now=_NOW)
    assert ev.created_at == _NOW


@pytest.mark.unit
def test_link_visual_evidence_id_is_stable():
    ev1 = link_visual_evidence(
        _SOURCE_FILE_ID, _IMAGE_ID,
        visual_region_id=_REGION_ID,
        blob_url=_BLOB_URL,
        text="region text",
        page_number=2,
        now=_NOW,
    )
    ev2 = link_visual_evidence(
        _SOURCE_FILE_ID, _IMAGE_ID,
        visual_region_id=_REGION_ID,
        blob_url=_BLOB_URL,
        text="region text",
        page_number=2,
        now=_NOW,
    )
    assert ev1.evidence_id == ev2.evidence_id


@pytest.mark.unit
def test_link_visual_evidence_returns_evidence_row():
    ev = link_visual_evidence(_SOURCE_FILE_ID, _IMAGE_ID, now=_NOW)
    assert isinstance(ev, EvidenceRow)


@pytest.mark.unit
def test_link_visual_evidence_callout_id_different_from_region_id():
    """figure_callout and image_region with different IDs should produce different evidence_ids."""
    ev_callout = link_visual_evidence(
        _SOURCE_FILE_ID, _IMAGE_ID,
        callout_id=_CALLOUT_ID,
        text="Callout A",
        now=_NOW,
    )
    ev_region = link_visual_evidence(
        _SOURCE_FILE_ID, _IMAGE_ID,
        visual_region_id=_REGION_ID,
        text="Region text",
        now=_NOW,
    )
    assert ev_callout.evidence_id != ev_region.evidence_id
    assert ev_callout.source_type == "figure_callout"
    assert ev_region.source_type == "image_region"


# ---------------------------------------------------------------------------
# Tests: end-to-end evidence FK chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_text_evidence_chunk_and_document_element_fks():
    """chunk evidence must carry both chunk_id and document_element_id FKs."""
    ev = link_text_evidence(
        _SOURCE_FILE_ID,
        chunk_id=_CHUNK_ID,
        document_element_id=_ELEMENT_ID,
        text="Evidence text",
        page_number=1,
        section_path="Procedures",
        now=_NOW,
    )
    assert ev.chunk_id == _CHUNK_ID
    assert ev.document_element_id == _ELEMENT_ID
    assert ev.source_type == "chunk"
    assert ev.page_number == 1
    assert ev.section_path == "Procedures"


@pytest.mark.unit
def test_visual_evidence_full_fk_chain():
    """image_region evidence must carry image_id, visual_region_id, and blob_url."""
    ev = link_visual_evidence(
        _SOURCE_FILE_ID,
        _IMAGE_ID,
        visual_region_id=_REGION_ID,
        blob_url=_BLOB_URL,
        text="OCR text from region",
        page_number=3,
        now=_NOW,
    )
    assert ev.image_id == _IMAGE_ID
    assert ev.visual_region_id == _REGION_ID
    assert ev.blob_url == _BLOB_URL
    assert ev.source_type == "image_region"
    assert ev.page_number == 3
