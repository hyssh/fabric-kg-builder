"""W4 synthetic fixtures: explicit, independently-reasoned expected outcomes.

Every fixture builder below returns a ``Fixture`` with:

- ``entities`` / ``relationships``: plain dataclasses (``FixtureEntity`` /
  ``FixtureRelationship``), converted to the project's pydantic
  ``EntityRow`` / ``RelationshipRow`` by the adapter helpers at the bottom
  of this module so the same fixture can feed the actual W2 hierarchy
  build entrypoint (direct import, no reimplementation).
- ``expected``: a dict of independently-reasoned assertions this QA
  workstream committed to *before* running any implementation code,
  per the "don't just reproduce implementation output as golden truth"
  instruction. A small number of fixtures also carry ``known_bug``/
  ``bug_citation`` documenting a defect previously found in
  ``graph/community.py`` (the legacy, non-Leiden community builder), kept
  as reference context only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from fabric_kg_builder.model.schemas import EntityRow, RelationshipRow

_FIXED_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)



@dataclass(frozen=True)
class FixtureEntity:
    entity_id: str
    entity_type: str = "org"
    display_name: str = ""
    description: str = ""
    evidence_ids: Optional[list[str]] = None


@dataclass(frozen=True)
class FixtureRelationship:
    relationship_id: str
    source_entity_id: str
    target_entity_id: str
    relationship_type: str = "related_to"
    evidence_ids: Optional[list[str]] = None


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    description: str
    entities: list[FixtureEntity]
    relationships: list[FixtureRelationship]
    expected: dict[str, Any] = field(default_factory=dict)
    known_bug: bool = False
    bug_citation: str = ""


# ---------------------------------------------------------------------------
# Adapters: FixtureEntity/FixtureRelationship -> project pydantic rows
# ---------------------------------------------------------------------------


def to_entity_row(fe: FixtureEntity) -> EntityRow:
    key = f"key:{fe.entity_id}"
    return EntityRow(
        entity_id=fe.entity_id,
        entity_type=fe.entity_type,
        display_name=fe.display_name or fe.entity_id,
        canonical_key=key,
        content_hash=key,
        created_at=_FIXED_TS,
        updated_at=_FIXED_TS,
        description=fe.description,
        evidence_ids=fe.evidence_ids,
    )


def to_relationship_row(fr: FixtureRelationship) -> RelationshipRow:
    # BUG-FIX (found by W4 self-testing, not present in upstream code):
    # content_hash is a *required* field on RelationshipRow; this adapter
    # previously omitted it entirely, so any fixture with relationships
    # raised pydantic ValidationError the moment materialize() was called.
    # No prior W4 test exercised materialize() on a relationship-bearing
    # fixture, so this went undetected until test_fake_leiden_provider_contract.py.
    return RelationshipRow(
        relationship_id=fr.relationship_id,
        relationship_type=fr.relationship_type,
        source_entity_id=fr.source_entity_id,
        target_entity_id=fr.target_entity_id,
        content_hash=f"key:{fr.relationship_id}",
        evidence_ids=fr.evidence_ids,
        created_at=_FIXED_TS,
    )


def materialize(fixture: Fixture) -> tuple[list[EntityRow], list[RelationshipRow]]:
    return (
        [to_entity_row(e) for e in fixture.entities],
        [to_relationship_row(r) for r in fixture.relationships],
    )


# ---------------------------------------------------------------------------
# F-LBL-IDENTICAL: many entities share one display_name. Observed:
# "twelve isolated entities with the same display name yielded 25 cluster
# rows but 14 unique cluster IDs".  Independently-reasoned expectation:
# cluster identity MUST derive from membership (sorted member entity_ids),
# never from label text alone, so N disjoint singleton/neighbor groups with
# an identical label must still mint N distinct cluster IDs when their
# membership differs.
# ---------------------------------------------------------------------------


def fixture_identical_labels(n: int = 12) -> Fixture:
    entities = [
        FixtureEntity(entity_id=f"e{i}", entity_type="org", display_name="Acme")
        for i in range(n)
    ]
    # No relationships: n isolated singleton components, all same label.
    return Fixture(
        fixture_id="F-LBL-IDENTICAL",
        description=f"{n} isolated entities sharing display_name 'Acme'.",
        entities=entities,
        relationships=[],
        expected={
            "unique_terminal_cluster_ids": n,
            "rationale": (
                "n disjoint singleton members must mint n distinct terminal "
                "cluster IDs; identity must key off membership, not label text."
            ),
        },
        known_bug=True,
        bug_citation=(
            "graph/community.py:_cluster_id derives id from "
            "label.lower()[:80] + domain_hash; identical labels collide "
            "regardless of differing membership."
        ),
    )


# ---------------------------------------------------------------------------
# F-LBL-LONG: label text exceeds the 80-char truncation window used by the
# current _cluster_id. Two entities whose full labels differ only after
# character 80 must still mint distinct cluster IDs if membership differs.
# ---------------------------------------------------------------------------


def fixture_long_labels() -> Fixture:
    prefix = "Z" * 85  # > 80 chars, identical up to the truncation point
    e1 = FixtureEntity(entity_id="e1", display_name=prefix + "-A")
    e2 = FixtureEntity(entity_id="e2", display_name=prefix + "-B")
    return Fixture(
        fixture_id="F-LBL-LONG",
        description="Two entities whose labels are identical in their first 80 characters.",
        entities=[e1, e2],
        relationships=[],
        expected={
            "unique_terminal_cluster_ids": 2,
            "rationale": "Distinct membership must mint distinct IDs even when label[:80] collides.",
        },
        known_bug=True,
        bug_citation="graph/community.py:_cluster_id truncates label to 80 chars before hashing.",
    )


# ---------------------------------------------------------------------------
# F-LBL-UNICODE: NFC vs NFD forms of the same visible label ("é" as one
# codepoint U+00E9 vs "e" + combining acute U+0065 U+0301). Per
# contracts/base.py canonical_json NFC-normalization contract, any
# downstream canonical hashing of these labels must treat them as equal
# after normalization; cluster *identity* (membership-based) must remain
# distinct regardless, since these are two different entities.
# ---------------------------------------------------------------------------


def fixture_unicode_labels() -> Fixture:
    nfc = "Caf\u00e9"  # Café (NFC, single codepoint é)
    nfd = "Cafe\u0301"  # Café (NFD, combining acute)
    e1 = FixtureEntity(entity_id="e1", display_name=nfc)
    e2 = FixtureEntity(entity_id="e2", display_name=nfd)
    return Fixture(
        fixture_id="F-LBL-UNICODE",
        description="One entity with an NFC label, another visually identical but NFD.",
        entities=[e1, e2],
        relationships=[],
        expected={
            "unique_terminal_cluster_ids": 2,
            "nfc_normalized_labels_equal": True,
            "rationale": (
                "Two distinct entities must mint two distinct cluster IDs "
                "even though their labels normalize (NFC) to the same string; "
                "canonical hashing of any *shared* payload must NFC-normalize "
                "per contracts/base.py, but identity is membership-based."
            ),
        },
    )


# ---------------------------------------------------------------------------
# F-PERM-NODES / F-PERM-RELS: same logical graph, nodes and relationships
# supplied in different input orders. Independently-reasoned expectation:
# the resulting membership sets per terminal cluster (as sets, not
# ordered lists) and the set of parent/child structural edges must be
# identical regardless of input order. Raw emission
# *order within a cluster's row list* was found NOT invariant under
# permutation for the actual native Leiden wrapper -- so this fixture
# explicitly tests the WEAKER, assignment-mandated invariant (membership
# sets / tree shape), not byte-identical row order, and documents that
# distinction instead of asserting the stronger claim as if proven.
# ---------------------------------------------------------------------------


def _permutation_base_entities() -> list[FixtureEntity]:
    return [
        FixtureEntity(entity_id=f"e{i}", display_name=f"Entity{i}", entity_type="product")
        for i in range(8)
    ]


def _permutation_base_relationships() -> list[FixtureRelationship]:
    pairs = [(0, 1), (1, 2), (2, 3), (4, 5), (5, 6), (6, 7)]
    return [
        FixtureRelationship(relationship_id=f"r{i}", source_entity_id=f"e{a}", target_entity_id=f"e{b}")
        for i, (a, b) in enumerate(pairs)
    ]


def fixture_node_permutation(reverse: bool = False) -> Fixture:
    entities = _permutation_base_entities()
    if reverse:
        entities = list(reversed(entities))
    return Fixture(
        fixture_id="F-PERM-NODES" + ("-REV" if reverse else "-FWD"),
        description="Same 8-entity graph with entity input order forward vs reversed.",
        entities=entities,
        relationships=_permutation_base_relationships(),
        expected={
            "connected_components_as_sets": [
                {"e0", "e1", "e2", "e3"},
                {"e4", "e5", "e6", "e7"},
            ],
            "rationale": "Component membership (as sets) must be order-invariant.",
        },
    )


def fixture_relationship_permutation(reverse: bool = False) -> Fixture:
    rels = _permutation_base_relationships()
    if reverse:
        rels = list(reversed(rels))
    return Fixture(
        fixture_id="F-PERM-RELS" + ("-REV" if reverse else "-FWD"),
        description="Same 6-edge graph with relationship input order forward vs reversed.",
        entities=_permutation_base_entities(),
        relationships=rels,
        expected={
            "connected_components_as_sets": [
                {"e0", "e1", "e2", "e3"},
                {"e4", "e5", "e6", "e7"},
            ],
            "rationale": "Component membership (as sets) must be order-invariant.",
        },
    )


def fixture_edgeless_isolates(n: int = 6) -> Fixture:
    entities = [FixtureEntity(entity_id=f"e{i}", display_name=f"Entity{i}") for i in range(n)]
    return Fixture(
        fixture_id="F-EDGELESS-ISOLATES",
        description=f"{n} entities, zero relationships.",
        entities=entities,
        relationships=[],
        expected={
            "terminal_cluster_count": n,
            "every_entity_is_singleton_terminal": True,
            "rationale": "Edgeless nonempty input retains every entity as an explicit singleton terminal.",
        },
    )


# ---------------------------------------------------------------------------
# F-ALL-COMPONENTS: three separate connected components of different sizes
# (not just the largest). Rule: "Process all connected
# components, not only LCC."
# ---------------------------------------------------------------------------


def fixture_all_components() -> Fixture:
    entities = [FixtureEntity(entity_id=f"e{i}") for i in range(9)]
    relationships = [
        # Component 1: e0-e1-e2-e3 (size 4, the LCC)
        FixtureRelationship(relationship_id="r0", source_entity_id="e0", target_entity_id="e1"),
        FixtureRelationship(relationship_id="r1", source_entity_id="e1", target_entity_id="e2"),
        FixtureRelationship(relationship_id="r2", source_entity_id="e2", target_entity_id="e3"),
        # Component 2: e4-e5 (size 2)
        FixtureRelationship(relationship_id="r3", source_entity_id="e4", target_entity_id="e5"),
        # Component 3: e6-e7-e8 (size 3)
        FixtureRelationship(relationship_id="r4", source_entity_id="e6", target_entity_id="e7"),
        FixtureRelationship(relationship_id="r5", source_entity_id="e7", target_entity_id="e8"),
    ]
    return Fixture(
        fixture_id="F-ALL-COMPONENTS",
        description="Three connected components of sizes 4, 2, 3 (no isolates).",
        entities=entities,
        relationships=relationships,
        expected={
            "connected_components_as_sets": [
                {"e0", "e1", "e2", "e3"},
                {"e4", "e5"},
                {"e6", "e7", "e8"},
            ],
            "all_9_entities_covered": True,
            "rationale": "All 3 components must be processed and covered, not just the largest.",
        },
    )


# F-OVERSIZED-UNSPLITTABLE: a clique (every node connected to every other)
# larger than a chosen max_cluster_size. Rule: "Never
# split an unsplittable terminal only to meet max_cluster_size. Record
# oversized-terminal diagnostics and do not promise a hard cluster-size cap."
# ---------------------------------------------------------------------------


def fixture_oversized_unsplittable_clique(size: int = 10, max_cluster_size: int = 4) -> Fixture:
    entities = [FixtureEntity(entity_id=f"k{i}") for i in range(size)]
    relationships = []
    rid = 0
    for i in range(size):
        for j in range(i + 1, size):
            relationships.append(
                FixtureRelationship(relationship_id=f"rk{rid}", source_entity_id=f"k{i}", target_entity_id=f"k{j}")
            )
            rid += 1
    return Fixture(
        fixture_id="F-OVERSIZED-UNSPLITTABLE",
        description=f"A {size}-node clique with max_cluster_size={max_cluster_size} (unsplittable without cutting real structure).",
        entities=entities,
        relationships=relationships,
        expected={
            "max_cluster_size_param": max_cluster_size,
            "terminal_may_exceed_max_cluster_size": True,
            "oversized_terminal_diagnostic_required": True,
            "rationale": "A fully-connected clique cannot be split below max_cluster_size without inventing a false cut; must be recorded as oversized, not forced apart.",
        },
    )
