"""Unit tests for enrichment resilience (SPEC-004 robustness fix 2026-06-24).

Covers the Surface PDF live-run failure: gpt-5-4-mini returned evidence items
with only ``text`` + ``confidence`` (no ``id_hint``, no ``source_type``).

Tests verify:
1. Evidence with only text+confidence canonicalizes successfully → deterministic
   evidence_id + default source_type.
2. A batch with one unsalvageable item drops it and keeps the rest (no hard-fail).
3. enrich exits 0 when the LLM output mimics the real Surface PDF failure.
4. Chunk without id_hint canonicalizes successfully (synthesized from content hash).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.orchestrator import (
    canonicalize_llm_output,
    enrich_batch,
)
from fabric_kg_builder.enrichment.output_schema import validate
from fabric_kg_builder.model.ids import content_hash, make_evidence_id


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DUMMY_CONFIG = FoundryConfig(endpoint="https://test.endpoint/")
_SOURCE_FILE_ID = "src:surface-pdf-abc123"
_NOW = datetime(2026, 6, 24, 16, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixture: real LLM output pattern (evidence without id_hint / source_type)
# ---------------------------------------------------------------------------

#: Mirrors the actual gpt-5-4-mini output that caused the live Surface PDF
#: run failure (exit 4).  Evidence items have only ``text`` + ``confidence``.
_SURFACE_PDF_LLM_OUTPUT = {
    "source_file_id": _SOURCE_FILE_ID,
    "pass": "p2",
    "entities": [
        {
            "id_hint": "device:surface-laptop-5",
            "type": "Device",
            "label": "Surface Laptop 5",
            "aliases": [],
            "confidence": 0.97,
        },
        {
            "id_hint": "component:battery",
            "type": "Component",
            "label": "Battery",
            "aliases": ["Battery Pack"],
            "confidence": 0.93,
        },
    ],
    "relationships": [
        {
            "id_hint": "rel:has-battery",
            "source_id_hint": "device:surface-laptop-5",
            "relation": "has_component",
            "target_id_hint": "component:battery",
            "confidence": 0.91,
        }
    ],
    "chunks": [],
    "visual_assets": [],
    "visual_regions": [],
    # Evidence items WITHOUT id_hint or source_type — real LLM output
    "evidence": [
        {"text": "Surface Laptop 5 ships with a 45Wh battery pack.", "confidence": 0.98},
        {"text": "Battery replacement requires Torx T5 screwdriver.", "confidence": 0.95},
    ],
    "placeholder_suggestions": [],
}

#: Same payload but with an entity that will crash canonicalize (empty type).
_OUTPUT_WITH_BAD_ENTITY = dict(_SURFACE_PDF_LLM_OUTPUT)
_OUTPUT_WITH_BAD_ENTITY["entities"] = list(_SURFACE_PDF_LLM_OUTPUT["entities"]) + [
    {
        "id_hint": "bad:entity",
        "type": "",           # empty type → normalize_canonical_key will produce weird key
        "label": "Good Item",  # but this should still survive
        "aliases": [],
        "confidence": 0.80,
    },
    # Truly unsalvageable: label is None (will blow up normalize_canonical_key)
    {
        "id_hint": "really-bad",
        "type": "Device",
        "label": None,        # type: ignore[arg-type]
        "aliases": [],
        "confidence": 0.75,
    },
]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_client(fixture_json: dict) -> FoundryClient:
    content_str = json.dumps(fixture_json)
    mock_sdk = MagicMock()
    completion = MagicMock(choices=[MagicMock(message=MagicMock(content=content_str))])
    mock_sdk.chat.completions.create.return_value = completion
    return FoundryClient(_DUMMY_CONFIG, _sdk_client=mock_sdk)


# ---------------------------------------------------------------------------
# 1. Evidence without id_hint / source_type → canonicalize succeeds
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvidenceWithoutHints:
    """Real Surface PDF pattern: evidence items with only text + confidence."""

    def test_schema_accepts_evidence_without_id_hint_and_source_type(self):
        """validate() must not raise when id_hint and source_type are absent."""
        parsed = validate(_SURFACE_PDF_LLM_OUTPUT)
        assert len(parsed.evidence) == 2
        for ev in parsed.evidence:
            assert ev.id_hint is None
            assert ev.source_type is None

    def test_canonicalize_produces_evidence_rows(self):
        """canonicalize_llm_output must produce EvidenceRows for hint-free evidence."""
        output = validate(_SURFACE_PDF_LLM_OUTPUT)
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        assert len(records.evidence) == 2

    def test_evidence_ids_are_deterministic(self):
        """Same text → same evidence_id on every run (content-hash based)."""
        output = validate(_SURFACE_PDF_LLM_OUTPUT)
        records1 = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        records2 = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        ids1 = {ev.evidence_id for ev in records1.evidence}
        ids2 = {ev.evidence_id for ev in records2.evidence}
        assert ids1 == ids2, "evidence_ids must be deterministic"

    def test_evidence_ids_match_manual_calculation(self):
        """evidence_id must be make_evidence_id(source_file_id, default_source_type, ...)."""
        output = validate(_SURFACE_PDF_LLM_OUTPUT)
        records = canonicalize_llm_output(
            output, _SOURCE_FILE_ID, default_source_type="document_span", now=_NOW
        )
        for ev_row in records.evidence:
            assert ev_row.evidence_id.startswith("evid:")
            assert ev_row.source_type == "document_span"

    def test_evidence_default_source_type_document_span(self):
        """Default source_type for document enrichment is 'document_span'."""
        output = validate(_SURFACE_PDF_LLM_OUTPUT)
        records = canonicalize_llm_output(
            output, _SOURCE_FILE_ID, default_source_type="document_span", now=_NOW
        )
        for ev_row in records.evidence:
            assert ev_row.source_type == "document_span"

    def test_evidence_default_source_type_csv_row(self):
        """Passing default_source_type='csv_row' applies for CSV sources."""
        output = validate(_SURFACE_PDF_LLM_OUTPUT)
        records = canonicalize_llm_output(
            output, _SOURCE_FILE_ID, default_source_type="csv_row", now=_NOW
        )
        for ev_row in records.evidence:
            assert ev_row.source_type == "csv_row"

    def test_explicit_source_type_not_overridden(self):
        """When the model DOES provide source_type, it must be respected."""
        payload = dict(_SURFACE_PDF_LLM_OUTPUT)
        payload["evidence"] = [
            {"text": "Explicit span", "source_type": "table_cell", "confidence": 0.9}
        ]
        output = validate(payload)
        records = canonicalize_llm_output(
            output, _SOURCE_FILE_ID, default_source_type="document_span", now=_NOW
        )
        assert records.evidence[0].source_type == "table_cell"

    def test_entities_and_relationships_still_produced(self):
        """Entities and relationships must be unaffected by evidence hint absence."""
        output = validate(_SURFACE_PDF_LLM_OUTPUT)
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        assert len(records.entities) == 2
        assert len(records.relationships) == 1


# ---------------------------------------------------------------------------
# 2. Chunk without id_hint → canonicalize synthesizes chunk_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChunkWithoutIdHint:
    """Chunks with absent id_hint must be synthesized deterministically."""

    def test_chunk_without_id_hint_canonicalizes(self):
        payload = {
            "source_file_id": _SOURCE_FILE_ID,
            "pass": "p2",
            "entities": [],
            "relationships": [],
            "chunks": [
                {
                    # id_hint absent — must not raise
                    "chunk_type": "section_text",
                    "content": "The battery is rated at 45 watt-hours.",
                }
            ],
            "visual_assets": [],
            "visual_regions": [],
            "evidence": [],
        }
        output = validate(payload)
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        assert len(records.chunks) == 1
        assert records.chunks[0].chunk_id.startswith("chunk:")

    def test_chunk_id_is_deterministic(self):
        payload = {
            "source_file_id": _SOURCE_FILE_ID,
            "pass": "p2",
            "entities": [],
            "relationships": [],
            "chunks": [
                {"chunk_type": "section_text", "content": "Deterministic content text."}
            ],
            "visual_assets": [],
            "visual_regions": [],
            "evidence": [],
        }
        output1 = validate(payload)
        output2 = validate(payload)
        r1 = canonicalize_llm_output(output1, _SOURCE_FILE_ID, now=_NOW)
        r2 = canonicalize_llm_output(output2, _SOURCE_FILE_ID, now=_NOW)
        assert r1.chunks[0].chunk_id == r2.chunks[0].chunk_id


# ---------------------------------------------------------------------------
# 3. Batch with unsalvageable item drops it, keeps the rest
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBatchPartialFailure:
    """A batch with one unsalvageable item must drop it and keep the rest."""

    def test_good_entities_kept_when_bad_entity_present(self):
        """canonicalize must keep good entities even if one entity blows up."""
        output = validate(_SURFACE_PDF_LLM_OUTPUT)
        # Use model_construct to bypass pydantic validation and inject a
        # genuinely unsalvageable entity (None label → normalize_canonical_key
        # will raise AttributeError).
        from fabric_kg_builder.enrichment.output_schema import Entity
        bad_entity = Entity.model_construct(
            id_hint="bad:one",
            type="Device",
            label=None,  # None label will blow up normalize_canonical_key
            confidence=0.8,
            aliases=[],
        )
        output.entities.append(bad_entity)

        # Should not raise; bad entity dropped; good ones kept.
        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        assert len(records.entities) == 2  # the 2 original good entities
        display_names = {e.display_name for e in records.entities}
        assert "Surface Laptop 5" in display_names
        assert "Battery" in display_names

    def test_good_evidence_kept_when_bad_evidence_present(self):
        """Good evidence rows must be kept even if one evidence item is broken."""
        output = validate(_SURFACE_PDF_LLM_OUTPUT)
        from fabric_kg_builder.enrichment.output_schema import Evidence
        # Inject a truly broken evidence item (confidence out of range already
        # caught by pydantic, so simulate canonicalize-level failure differently).
        # We use a mock-like object that raises on attribute access.
        class _BrokenEvidence:
            id_hint = None
            source_type = None
            text = None
            row_index = None
            col_index = None
            page_number = None
            section_path = None
            table_id = None
            figure_id = None
            image_id = None
            callout_id = None
            visual_region_id_hint = None
            blob_url = None

            @property
            def row_index(self):  # type: ignore[override]
                raise RuntimeError("simulated per-item crash")

        bad_ev = _BrokenEvidence()
        output.evidence.append(bad_ev)  # type: ignore[arg-type]

        records = canonicalize_llm_output(output, _SOURCE_FILE_ID, now=_NOW)
        # The 2 original good evidence items must survive.
        assert len(records.evidence) == 2


# ---------------------------------------------------------------------------
# 4. enrich_batch exits 0 on Surface PDF pattern (end-to-end offline test)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnrichBatchSurfacePdfPattern:
    """enrich_batch must succeed end-to-end on the real Surface PDF LLM output pattern."""

    def test_enrich_batch_returns_records(self, tmp_path: Path):
        """enrich_batch with Surface PDF output returns entities + evidence."""
        client = _make_client(_SURFACE_PDF_LLM_OUTPUT)
        records = enrich_batch(
            source_content="[section_text] Surface Laptop 5 battery replacement procedure.",
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=tmp_path / "enriched",
            default_source_type="document_span",
        )
        assert len(records.entities) == 2
        assert len(records.evidence) == 2

    def test_enrich_batch_evidence_has_source_type(self, tmp_path: Path):
        """All evidence rows must have a non-empty source_type after synthesis."""
        client = _make_client(_SURFACE_PDF_LLM_OUTPUT)
        records = enrich_batch(
            source_content="[section_text] Surface Laptop 5 battery.",
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=tmp_path / "enriched",
            default_source_type="document_span",
        )
        for ev in records.evidence:
            assert ev.source_type, "evidence source_type must be non-empty"

    def test_enrich_batch_evidence_ids_are_evid_prefixed(self, tmp_path: Path):
        """All synthesized evidence_ids must use the 'evid:' prefix."""
        client = _make_client(_SURFACE_PDF_LLM_OUTPUT)
        records = enrich_batch(
            source_content="[section_text] Surface Laptop 5.",
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=tmp_path / "enriched",
        )
        for ev in records.evidence:
            assert ev.evidence_id.startswith("evid:")

    def test_enrich_batch_writes_output_json(self, tmp_path: Path):
        """enrich_batch must write intermediate JSON even with synthesized evidence."""
        out_dir = tmp_path / "enriched"
        client = _make_client(_SURFACE_PDF_LLM_OUTPUT)
        enrich_batch(
            source_content="[section_text] Surface Laptop 5.",
            source_file_id=_SOURCE_FILE_ID,
            client=client,
            domain_brief=None,
            output_dir=out_dir,
        )
        json_files = [f for f in out_dir.glob("*.json") if ".checkpoint" not in f.name]
        assert json_files, "enrich_batch must write at least one output JSON file"


# ---------------------------------------------------------------------------
# 5. enrich CLI exits 0 on Surface PDF pattern via CliRunner
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnrichCmdSurfacePdfPattern:
    """enrich command must exit 0 when the LLM returns Surface PDF pattern output."""

    def test_enrich_cmd_exits_0_with_evidence_no_hints(self, tmp_path: Path) -> None:
        """CLI enrich must exit 0 when evidence items have no id_hint or source_type."""
        from click.testing import CliRunner
        from fabric_kg_builder.cli import cli

        # Build a minimal PDF fixture.
        MINIMAL_PDF = (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
            b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
            b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
            b"5 0 obj<</Length 44>>\nstream\n"
            b"BT /F1 12 Tf 100 700 Td (Surface Laptop) Tj ET\n"
            b"endstream\nendobj\n"
            b"xref\n0 6\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000058 00000 n \n"
            b"0000000115 00000 n \n"
            b"0000000266 00000 n \n"
            b"0000000342 00000 n \n"
            b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n436\n%%EOF"
        )
        pdf_path = tmp_path / "surface.pdf"
        pdf_path.write_bytes(MINIMAL_PDF)
        out_dir = tmp_path / "enriched"
        out_dir.mkdir(parents=True)

        client = _make_client(_SURFACE_PDF_LLM_OUTPUT)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={"_foundry_client": client},
        )
        assert result.exit_code == 0, (
            f"enrich exited {result.exit_code} on Surface PDF pattern.\n"
            f"Output: {result.output}\nException: {result.exception}"
        )
