"""Tests for deterministic ID generation and canonical_key normalization.

Verifies:
- Same inputs always produce the same ID (stability)
- Different inputs produce different IDs (no collisions for obvious cases)
- canonical_key normalization follows SPEC-002 §5.2 rules exactly
- All 8 ID constructors produce correctly prefixed output
"""

import pytest
from fabric_kg_builder.model.ids import (
    content_hash,
    make_chunk_id,
    make_document_element_id,
    make_entity_id,
    make_evidence_id,
    make_id,
    make_image_id,
    make_relationship_id,
    make_source_file_id,
    make_visual_region_id,
    normalize_canonical_key,
)


# ---------------------------------------------------------------------------
# make_id — core primitive
# ---------------------------------------------------------------------------


def test_make_id_format():
    result = make_id("entity", "device:surface-laptop-5")
    prefix, digest = result.split(":", 1)
    assert prefix == "entity"
    assert len(digest) == 32
    assert all(c in "0123456789abcdef" for c in digest)


def test_make_id_deterministic():
    assert make_id("entity", "device:surface-laptop-5") == make_id("entity", "device:surface-laptop-5")


def test_make_id_different_inputs_differ():
    assert make_id("entity", "device:surface-laptop-5") != make_id("entity", "device:surface-laptop-4")


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


def test_content_hash_is_64_hex_chars():
    h = content_hash("hello world")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_content_hash_deterministic():
    assert content_hash("abc") == content_hash("abc")


def test_content_hash_differs():
    assert content_hash("abc") != content_hash("ABC")


# ---------------------------------------------------------------------------
# normalize_canonical_key  (SPEC-002 §5.2)
# ---------------------------------------------------------------------------

CANONICAL_KEY_CASES = [
    # (entity_type, display_name, expected)
    ("Device", "Surface Laptop 5", "device:surface-laptop-5"),
    ("Component", "Battery Pack", "component:battery-pack"),
    ("PartNumber", "M1287099-003", "partnumber:m1287099-003"),
    # leading/trailing whitespace stripped
    ("Part", "  Widget  ", "part:widget"),
    # internal whitespace collapsed then → dash
    ("Step", "Install  the  Drive", "step:install-the-drive"),
    # special chars removed (except - and space)
    ("Device", "Surface (Pro) 9!", "device:surface-pro-9"),
    # entity_type lowercased
    ("PROCEDURE", "Boot Sequence", "procedure:boot-sequence"),
    # already lowercase, no change
    ("component", "battery-pack", "component:battery-pack"),
]


@pytest.mark.parametrize("entity_type,display_name,expected", CANONICAL_KEY_CASES)
def test_normalize_canonical_key(entity_type, display_name, expected):
    assert normalize_canonical_key(entity_type, display_name) == expected


def test_canonical_key_stability():
    """Same input gives same canonical_key across calls."""
    k1 = normalize_canonical_key("Device", "Surface Laptop 5")
    k2 = normalize_canonical_key("Device", "Surface Laptop 5")
    assert k1 == k2


# ---------------------------------------------------------------------------
# Per-table ID constructors
# ---------------------------------------------------------------------------


def test_make_entity_id_prefix_and_stability():
    eid = make_entity_id("Device", "Surface Laptop 5")
    assert eid.startswith("entity:")
    assert eid == make_entity_id("Device", "Surface Laptop 5")


def test_make_entity_id_determinism_via_canonical_key():
    # Different case/whitespace that normalises to the same canonical_key → same ID
    assert make_entity_id("Device", "Surface Laptop 5") == make_entity_id("device", "surface laptop 5")


def test_make_source_file_id():
    sfid = make_source_file_id("examples/csv/sample.csv", "deadbeef1234")
    assert sfid.startswith("src:")
    assert sfid == make_source_file_id("examples/csv/sample.csv", "deadbeef1234")


def test_make_document_element_id():
    h = content_hash("row content")
    did = make_document_element_id("src:abc", "table_row", 1, 0, h)
    assert did.startswith("elem:")
    assert did == make_document_element_id("src:abc", "table_row", 1, 0, h)


def test_make_document_element_id_none_page_sort():
    # None values must not cause an error
    h = content_hash("text")
    did = make_document_element_id("src:abc", "section", None, None, h)
    assert did.startswith("elem:")


def test_make_chunk_id():
    h = content_hash("chunk text")
    cid = make_chunk_id("src:abc", "section_text", h)
    assert cid.startswith("chunk:")
    assert cid == make_chunk_id("src:abc", "section_text", h)


def test_make_relationship_id():
    rid = make_relationship_id("has_component", "entity:aaa", "entity:bbb")
    assert rid.startswith("rel:")
    assert rid == make_relationship_id("has_component", "entity:aaa", "entity:bbb")


def test_make_evidence_id():
    h = content_hash("evidence text")
    eid = make_evidence_id("src:abc", "csv_row", "page1:row3:col2", h)
    assert eid.startswith("evid:")
    assert eid == make_evidence_id("src:abc", "csv_row", "page1:row3:col2", h)


def test_make_image_id():
    iid = make_image_id("src:abc", "cafebabe1234")
    assert iid.startswith("img:")
    assert iid == make_image_id("src:abc", "cafebabe1234")


def test_make_visual_region_id():
    vrid = make_visual_region_id("img:xyz", "callout", "Battery A", 0)
    assert vrid.startswith("vr:")
    assert vrid == make_visual_region_id("img:xyz", "callout", "Battery A", 0)


def test_make_visual_region_id_none_label():
    vrid = make_visual_region_id("img:xyz", "ocr_text", None, 3)
    assert vrid.startswith("vr:")


def test_different_prefix_same_canonical_different_ids():
    """Changing only the prefix changes the final ID."""
    assert make_id("entity", "x") != make_id("chunk", "x")
