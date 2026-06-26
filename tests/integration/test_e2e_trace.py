"""End-to-end traceability test — SPEC-005 §7 / PRD §23.13.

Verifies that a single entity → relationship → visual evidence chain can be
traced end-to-end via FK joins, and that the same entity surfaces in a derived
AI Search document via the linkage module.

Trace path:
  entities[entity_id]
    ← relationships[source_entity_id / target_entity_id]
      → relationships[evidence_id]
        → evidence[evidence_id]
          → evidence[visual_region_id]
            → visual_regions[visual_region_id]
              → visual_regions[image_id]
                → visual_assets[image_id]

And separately:
  entity + chunks → derive_chunk_doc() → AI Search document contains entity_id

Fixture: tests/fixtures/e2e_trace/{entities,relationships,visual_assets,visual_regions,evidence}.json
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixture data loader
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "e2e_trace"


def _load(name: str) -> list[dict]:
    return json.loads((_FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def e2e_tables() -> dict[str, list[dict]]:
    """Load all e2e trace fixture tables as plain list-of-dicts."""
    return {
        "entities": _load("entities"),
        "relationships": _load("relationships"),
        "visual_assets": _load("visual_assets"),
        "visual_regions": _load("visual_regions"),
        "evidence": _load("evidence"),
    }


# ---------------------------------------------------------------------------
# Helper: simple FK join on plain list-of-dicts
# ---------------------------------------------------------------------------


def _find(rows: list[dict], **where) -> list[dict]:
    """Filter rows where all key=value pairs match."""
    result = []
    for row in rows:
        if all(row.get(k) == v for k, v in where.items()):
            result.append(row)
    return result


# ---------------------------------------------------------------------------
# TC-AC-13: Entity → Relationship → Evidence trace
# ---------------------------------------------------------------------------


class TestE2ETrace:
    def test_entity_relationship_visual_evidence_trace(self, e2e_tables: dict):
        """
        Trace: Surface Laptop 5 --has_component--> Battery
               evidenced by figure12 callout B (visual)

        Steps:
        1. Find the has_component relationship
        2. Both endpoints exist in entities
        3. Follow evidence_id to evidence table
        4. Follow visual_region_id to visual_regions
        5. Follow image_id to visual_assets — confirm blob_url matches
        """
        entities = e2e_tables["entities"]
        relationships = e2e_tables["relationships"]
        visual_assets = e2e_tables["visual_assets"]
        visual_regions = e2e_tables["visual_regions"]
        evidence = e2e_tables["evidence"]

        # Step 1: find the relationship
        rels = _find(relationships, relationship_type="has_component")
        assert len(rels) == 1, f"Expected exactly one has_component relationship, got {len(rels)}"
        rel = rels[0]

        # Step 2: confirm both endpoints exist in entities
        entity_ids = {e["entity_id"] for e in entities}
        source_id = rel["source_entity_id"]
        target_id = rel["target_entity_id"]
        assert source_id in entity_ids, f"source_entity_id '{source_id}' not in entities"
        assert target_id in entity_ids, f"target_entity_id '{target_id}' not in entities"

        # Step 3: follow evidence_id to evidence table
        ev_id = rel["evidence_id"]
        assert ev_id is not None, "relationship has no evidence_id"
        ev_rows = _find(evidence, evidence_id=ev_id)
        assert len(ev_rows) == 1, f"Expected one evidence row for evidence_id={ev_id!r}"
        ev = ev_rows[0]
        assert ev["source_type"] == "figure_callout"
        assert ev["blob_url"] is not None, "evidence.blob_url must be non-null for visual evidence"

        # Step 4: follow visual_region_id to visual_regions
        vr_id = ev["visual_region_id"]
        assert vr_id is not None, "evidence.visual_region_id must be non-null for figure_callout"
        vr_rows = _find(visual_regions, visual_region_id=vr_id)
        assert len(vr_rows) == 1, f"Expected one visual_region for visual_region_id={vr_id!r}"
        vr = vr_rows[0]
        assert vr["region_type"] == "callout"
        assert vr["identified_entity_id"] == target_id, (
            f"visual_region.identified_entity_id should be {target_id!r}, got {vr['identified_entity_id']!r}"
        )

        # Step 5: follow image_id to visual_assets
        img_id = vr["image_id"]
        va_rows = _find(visual_assets, image_id=img_id)
        assert len(va_rows) == 1, f"Expected one visual_asset for image_id={img_id!r}"
        va = va_rows[0]
        assert va["blob_url"] is not None, "visual_asset.blob_url must be non-null"
        assert va["blob_url"] == ev["blob_url"], (
            f"blob_url mismatch: evidence has {ev['blob_url']!r}, visual_asset has {va['blob_url']!r}"
        )

    def test_no_null_fk_values_in_chain(self, e2e_tables: dict):
        """All FK columns in the trace chain must be non-null (backbone integrity)."""
        rel = e2e_tables["relationships"][0]
        assert rel["source_entity_id"] is not None
        assert rel["target_entity_id"] is not None
        assert rel["evidence_id"] is not None

        ev = e2e_tables["evidence"][0]
        assert ev["evidence_id"] is not None
        assert ev["visual_region_id"] is not None

        vr = e2e_tables["visual_regions"][0]
        assert vr["visual_region_id"] is not None
        assert vr["image_id"] is not None

        va = e2e_tables["visual_assets"][0]
        assert va["image_id"] is not None
        assert va["blob_url"] is not None

    def test_source_entity_display_name(self, e2e_tables: dict):
        """The source entity has the expected display_name."""
        source_id = "e2e:device:surface-laptop-5"
        entities = _find(e2e_tables["entities"], entity_id=source_id)
        assert len(entities) == 1
        assert entities[0]["display_name"] == "Surface Laptop 5"

    def test_target_entity_canonical_key(self, e2e_tables: dict):
        """The target entity (Battery) has a canonical_key set."""
        target_id = "e2e:component:battery"
        entities = _find(e2e_tables["entities"], entity_id=target_id)
        assert len(entities) == 1
        assert entities[0]["canonical_key"] == "surface-laptop-5:battery"


# ---------------------------------------------------------------------------
# AI Search linkage trace via search.linkage
# ---------------------------------------------------------------------------


class TestSearchLinkageTrace:
    """Verify that the same entity surfaces in a derived AI Search document.

    Uses derive_chunk_doc() to produce a search document from a chunk that
    references the source entity, then asserts entity_ids and entity_aliases
    are correctly populated.
    """

    def _make_chunk(self, entity_id: str) -> dict:
        return {
            "chunk_id": "ch:e2e:surface-laptop-5:desc",
            "source_file_id": "sf:e2e:sample.csv",
            "document_element_id": "de:e2e:001",
            "chunk_type": "section_text",
            "content": "Surface Laptop 5 features a rechargeable battery.",
            "embedding_text": "Surface Laptop 5 features a rechargeable battery.",
            "blob_url": None,
            "related_entity_ids": [entity_id],
            "entity_search_keys": None,  # will be derived from entity lookup
            "content_hash": "abc",
            "created_at": None,
        }

    def test_entity_surfaces_in_search_doc(self, e2e_tables: dict):
        """derive_chunk_doc produces a doc where entity_ids contains the source entity."""
        from fabric_kg_builder.search.linkage import (  # noqa: PLC0415
            build_entity_lookup,
            derive_chunk_doc,
        )

        entity_id = "e2e:device:surface-laptop-5"
        entities_by_id = build_entity_lookup(e2e_tables["entities"])
        chunk = self._make_chunk(entity_id)

        doc = derive_chunk_doc(chunk, entities_by_id)

        assert entity_id in doc["entity_ids"], (
            f"Expected entity_id {entity_id!r} in doc['entity_ids']={doc['entity_ids']!r}"
        )
        assert doc["canonical_key"] == "surface-laptop-5", (
            f"Expected canonical_key='surface-laptop-5', got {doc['canonical_key']!r}"
        )
        # entity_aliases should be derived from entity.search_aliases
        assert isinstance(doc["entity_aliases"], list)
        assert len(doc["entity_aliases"]) > 0, "entity_aliases must be non-empty"

    def test_entity_type_in_search_doc(self, e2e_tables: dict):
        """entity_types in the derived search doc reflects the entity type."""
        from fabric_kg_builder.search.linkage import (  # noqa: PLC0415
            build_entity_lookup,
            derive_chunk_doc,
        )

        entity_id = "e2e:device:surface-laptop-5"
        entities_by_id = build_entity_lookup(e2e_tables["entities"])
        chunk = self._make_chunk(entity_id)

        doc = derive_chunk_doc(chunk, entities_by_id)

        assert "Device" in doc["entity_types"], (
            f"Expected 'Device' in entity_types, got {doc['entity_types']!r}"
        )


# ---------------------------------------------------------------------------
# Data gates pass on clean e2e fixture
# ---------------------------------------------------------------------------


class TestDataGatesOnE2EFixture:
    """The e2e fixture data must pass all data integrity gates (VAL-001..012)."""

    def test_run_gates_returns_no_violations(self, e2e_tables: dict):
        from fabric_kg_builder.validate.data_gates import run_gates  # noqa: PLC0415

        # Build a combined table dict with only the tables that exist in the fixture
        violations = run_gates(e2e_tables)
        assert violations == [], (
            f"Expected no data gate violations on clean e2e fixture, got:\n"
            + "\n".join(str(v) for v in violations)
        )

    def test_suite_gates_return_no_fails(self, e2e_tables: dict):
        from fabric_kg_builder.validate.suite import (  # noqa: PLC0415
            _val008_chunk_docelem_fk,
            _val010_blob_url_present,
            _val028_polygon_json,
        )

        # The e2e fixture doesn't have chunks or document_elements,
        # so VAL-008 is vacuously true.  VAL-010 checks visual_assets.
        val010 = _val010_blob_url_present(e2e_tables)
        assert val010 == [], f"Expected no VAL-010 violations on e2e fixture: {val010}"

        val028 = _val028_polygon_json(e2e_tables)
        assert val028 == [], f"Expected no VAL-028 violations on e2e fixture: {val028}"
