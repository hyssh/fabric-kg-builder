"""W4 real-native corpus: structural-validity assertions against the
*actual* ``graspologic-native==1.2.5`` distribution, run through the real
``build_derived_community_hierarchy`` entrypoint with ``NativeLeidenProvider``
(no scripted/fake provider in this file).

Scope and honesty guard
-----------------------
This module asserts **structural validity** only: reproducibility for a
fixed seed, multi-level (``native_depth``/``structural_height`` > 0)
output actually occurring for at least one input shape, zero
``validate_derived_hierarchy`` findings, exactly-once terminal membership
coverage, full-length (64 hex char / SHA-256) execution fingerprints, and
the independently-reasoned oversized-unsplittable positive control (see
``fixtures.fixture_oversized_unsplittable_clique``) going end-to-end
through the real native call. It never asserts *which* communities Leiden
chooses, i.e. no semantic/clustering-quality claim is made anywhere here
— that would require ground-truth community labels this workstream has
no authority to assert ("don't invent ... semantic quality claims" per
the governing mandate).

This file only runs when ``graspologic_native`` is importable **and**
the installed distribution version is exactly ``1.2.5`` (the pin
``NativeLeidenProvider`` was written against, per
``community_contracts.NATIVE_PACKAGE_VERSION``). On any other host
(dependency absent, or a different version present) every test in this
module is **skipped**, never silently passed or failed — this is a
deliberate corollary of the mandate's "absence of leidenalg/igraph does
not establish production provider availability" instruction, applied in
reverse: *presence* of some other package/version must not be treated
as the pinned native either. A skip here is not proof of native-backed
behavior; only a full pass with ``skipped == 0`` is.
"""

from __future__ import annotations

import importlib.metadata

import pytest

from fabric_kg_builder.graph.community_contracts import (
    NATIVE_PACKAGE_NAME,
    NATIVE_PACKAGE_VERSION,
)
from fabric_kg_builder.graph.community_hierarchy_v2 import (
    build_derived_community_hierarchy,
)
from fabric_kg_builder.graph.community_validation import validate_derived_hierarchy

from .fixtures import (
    Fixture,
    FixtureEntity,
    FixtureRelationship,
    fixture_all_components,
    fixture_edgeless_isolates,
    fixture_identical_labels,
    fixture_long_labels,
    fixture_node_permutation,
    fixture_oversized_unsplittable_clique,
    fixture_relationship_permutation,
    fixture_unicode_labels,
    materialize,
)

gn = pytest.importorskip("graspologic_native", reason="graspologic_native not importable on this host")

try:
    _installed_version = importlib.metadata.version(NATIVE_PACKAGE_NAME)
except importlib.metadata.PackageNotFoundError:
    _installed_version = None

if _installed_version != NATIVE_PACKAGE_VERSION:
    pytest.skip(
        f"installed '{NATIVE_PACKAGE_NAME}' version is "
        f"{_installed_version!r}, not the exactly-pinned "
        f"{NATIVE_PACKAGE_VERSION!r} these tests require; skipping rather "
        "than asserting pinned-native behavior against an unverified "
        "version.",
        allow_module_level=True,
    )

# Import only after the version gate above — NativeLeidenProvider itself
# defers the real import to first ``partition()`` call, but constructing
# it unconditionally at module scope on a mismatched-version host would
# still be safe; this ordering keeps the gate as the single source of
# truth for whether anything in this file exercises the real package.
from fabric_kg_builder.graph.community_provider import NativeLeidenProvider  # noqa: E402

_FIXED_TS_KWARGS = dict(policy_id="w4-real-native-corpus", config_id="w4-real-native")


def _fresh_provider() -> NativeLeidenProvider:
    # A fresh instance per call: NativeLeidenProvider caches the imported
    # module on first use (``self._module``), but carries no partition
    # state, so reuse would be safe too — fresh instances just keep each
    # test's provider lifecycle independent and easy to reason about.
    return NativeLeidenProvider()


def test_native_provider_identity_matches_pin() -> None:
    """``provider_id``/``provider_version`` on the real provider must be
    exactly the pinned identity this whole corpus is scoped to — this is
    the one assertion in this file that is actually about the native
    dependency itself, not the graph it is run against."""
    provider = _fresh_provider()
    assert provider.provider_id == "leiden_native"
    assert provider.provider_version == NATIVE_PACKAGE_VERSION == "1.2.5"


def _chain_fixture(n: int = 30):
    """A plain path graph (no cycles, no branching): c0-c1-c2-...-c(n-1).
    Independently chosen (not copied from any parent-supplied fixture) to
    empirically force genuine multi-level native output at
    ``max_cluster_size=5`` — verified ad hoc against the real interpreter
    before being committed here (level 0 has 6 clusters, level 1 has 8,
    30 entities fully covered, zero validation findings)."""
    from .fixtures import Fixture, FixtureEntity, FixtureRelationship

    nodes = [f"c{i}" for i in range(n)]
    entities = [FixtureEntity(entity_id=node, evidence_ids=[f"ev-{node}"]) for node in nodes]
    relationships = [
        FixtureRelationship(
            relationship_id=f"rel-{i}",
            source_entity_id=nodes[i],
            target_entity_id=nodes[i + 1],
            evidence_ids=[f"ev-rel-{i}"],
        )
        for i in range(n - 1)
    ]
    return Fixture(
        fixture_id="F-REAL-NATIVE-CHAIN-30",
        description="30-node path graph; empirically forces native_depth>0 under max_cluster_size=5.",
        entities=entities,
        relationships=relationships,
    )


def test_real_native_chain_produces_multilevel_structurally_valid_hierarchy() -> None:
    """End-to-end real-native run through the actual
    ``build_derived_community_hierarchy`` entrypoint. Asserts structural
    properties only: COMPLETE status, full entity/membership coverage,
    genuine multi-level output (``native_depth`` taking more than one
    distinct value), zero structural/evidence/coverage validation
    findings, and a full 64-hex-char (SHA-256) execution fingerprint
    (the corrected fullSha256 contract — not a truncated/prefixed32
    identifier)."""
    fixture = _chain_fixture(n=30)
    entities, relationships = materialize(fixture)

    outcome = build_derived_community_hierarchy(
        entities,
        relationships,
        provider=_fresh_provider(),
        run_id="w4-real-native-chain-run",
        max_cluster_size=5,
        seed=999,
        **_FIXED_TS_KWARGS,
    )

    assert outcome.status.name == "COMPLETE"
    assert outcome.provider_id == "leiden_native"
    assert outcome.provider_version == "1.2.5"

    report = validate_derived_hierarchy(outcome)
    assert report.is_valid, report.findings

    native_depths = {c.native_depth for c in outcome.clusters}
    structural_heights = {c.structural_height for c in outcome.clusters}
    assert len(native_depths) > 1, (
        "expected genuine multi-level native output (native_depth taking "
        f"more than one value); got {sorted(native_depths)} — this chain "
        "fixture was empirically verified to split at max_cluster_size=5/"
        "seed=999 before being committed, so a single-level result here "
        "means real-native behavior drifted from the verified baseline."
    )
    assert len(structural_heights) > 1

    terminal_cluster_ids = {c.cluster_id for c in outcome.clusters if c.is_terminal}
    terminal_member_entity_ids = {
        m.entity_id for m in outcome.memberships if m.cluster_id in terminal_cluster_ids
    }
    assert terminal_member_entity_ids == {e.entity_id for e in entities}

    assert outcome.execution_fingerprint is not None
    assert len(outcome.execution_fingerprint) == 64
    assert all(ch in "0123456789abcdef" for ch in outcome.execution_fingerprint)


def test_real_native_chain_is_reproducible_for_fixed_seed() -> None:
    """Two independent real-native builds over the identical graph/config/
    seed must agree on cluster identity, membership, depth/height, AND
    ``execution_fingerprint`` byte-for-byte. This is a reproducibility
    claim only (same seed -> same output), never a claim about which
    communities are "correct"."""
    fixture = _chain_fixture(n=30)
    entities, relationships = materialize(fixture)

    def _run():
        return build_derived_community_hierarchy(
            entities,
            relationships,
            provider=_fresh_provider(),
            run_id="w4-real-native-repro-run",
            max_cluster_size=5,
            seed=999,
            **_FIXED_TS_KWARGS,
        )

    outcome_a = _run()
    outcome_b = _run()

    def _snapshot(outcome):
        return sorted(
            (
                c.cluster_id,
                c.parent_cluster_id,
                tuple(sorted(c.member_entity_ids)),
                c.native_depth,
                c.structural_height,
            )
            for c in outcome.clusters
        )

    assert _snapshot(outcome_a) == _snapshot(outcome_b)
    assert outcome_a.execution_fingerprint == outcome_b.execution_fingerprint


def test_real_native_oversized_unsplittable_clique_is_flagged_not_rejected() -> None:
    """Independently-reasoned positive control (round-5 instruction: keep
    oversized-unsplittable as REQUIRED behavior, not a defect), run
    end-to-end against the real native provider rather than a scripted
    one, using the existing ``fixture_oversized_unsplittable_clique``
    fixture verbatim (a single fully-connected clique larger than
    ``max_cluster_size``). A fully-connected clique cannot be split
    without inventing a false cut, so it must surface as exactly one
    *flagged* oversized terminal cluster — build must still COMPLETE, not
    reject the input. (Separately verified ad hoc against the real
    interpreter with TWO disjoint 15-cliques -> 2 oversized terminal
    clusters; not re-asserted here since that shape is not what this
    shared fixture builds.)"""
    fixture = fixture_oversized_unsplittable_clique(size=15, max_cluster_size=6)
    entities, relationships = materialize(fixture)

    outcome = build_derived_community_hierarchy(
        entities,
        relationships,
        provider=_fresh_provider(),
        run_id="w4-real-native-oversized-run",
        max_cluster_size=6,
        seed=12345,
        **_FIXED_TS_KWARGS,
    )

    assert outcome.status.name == "COMPLETE"
    assert len(outcome.oversized_terminal_cluster_ids) == 1
    (cluster_id,) = outcome.oversized_terminal_cluster_ids
    matching = [c for c in outcome.clusters if c.cluster_id == cluster_id]
    assert len(matching) == 1
    assert matching[0].is_terminal
    assert len(matching[0].member_entity_ids) == 15

    report = validate_derived_hierarchy(outcome)
    assert report.is_valid, report.findings


def test_real_native_edgeless_isolates_each_entity_is_own_singleton_terminal() -> None:
    """``fixture_edgeless_isolates`` (nonempty entities, zero relationships)
    run through the real native entrypoint. Per the hierarchy rules
    ("retains every entity, without invented topic layers"): the real
    build must report ``INSUFFICIENT`` (not an error, not COMPLETE — no
    connecting relationship exists for Leiden to partition on) and every
    entity must still land in its own singleton terminal cluster, never
    merged into a synthetic grouping."""
    fixture = fixture_edgeless_isolates(n=6)
    entities, relationships = materialize(fixture)

    outcome = build_derived_community_hierarchy(
        entities,
        relationships,
        provider=_fresh_provider(),
        run_id="w4-real-native-isolates-run",
        max_cluster_size=10,
        seed=1,
        **_FIXED_TS_KWARGS,
    )

    assert outcome.status.name == "INSUFFICIENT"
    terminals = [c for c in outcome.clusters if c.is_terminal]
    assert len(terminals) == fixture.expected["terminal_cluster_count"] == 6
    assert all(len(c.member_entity_ids) == 1 for c in terminals)
    assert {e.entity_id for e in entities} == {m for c in terminals for m in c.member_entity_ids}

    report = validate_derived_hierarchy(outcome)
    assert report.is_valid, report.findings


def test_real_native_all_connected_components_are_processed_not_only_largest() -> None:
    """``fixture_all_components`` (three disjoint components of sizes
    4/2/3, no isolates) run through the real native entrypoint. Per
    the hierarchy rule ("process all connected components, not only
    LCC"): all 9 entities across all 3 components must be covered by
    terminal clusters, with membership-as-sets matching each component
    exactly — real native output, not a scripted/reference assertion."""
    fixture = fixture_all_components()
    entities, relationships = materialize(fixture)

    outcome = build_derived_community_hierarchy(
        entities,
        relationships,
        provider=_fresh_provider(),
        run_id="w4-real-native-all-components-run",
        max_cluster_size=10,
        seed=1,
        **_FIXED_TS_KWARGS,
    )

    assert outcome.status.name == "COMPLETE"
    terminals = [c for c in outcome.clusters if c.is_terminal]
    terminal_member_sets = {frozenset(c.member_entity_ids) for c in terminals}
    expected_sets = {frozenset(s) for s in fixture.expected["connected_components_as_sets"]}
    assert terminal_member_sets == expected_sets
    all_covered = {m for c in terminals for m in c.member_entity_ids}
    assert all_covered == {e.entity_id for e in entities}

    report = validate_derived_hierarchy(outcome)
    assert report.is_valid, report.findings


def test_real_native_reciprocal_pair_and_self_loop_evidence_attribution() -> None:
    """A 2-entity graph with a self-loop (A->A) plus a reciprocal pair
    (A->B and B->A), each relationship carrying distinct evidence, run
    through the real native entrypoint. Independently reasoned per
    the hierarchy rule ("preserve self-relations in original knowledge,
    exclude them from partition edges with a recorded reason") plus the
    F-DUP-RECIPROCAL invariant (reciprocal rows collapse onto one
    undirected projection edge, never doubling weight): the self-loop
    must be recorded as an excluded-from-partition diagnostic (never
    silently dropped), and ALL THREE relationships' evidence (self-loop
    plus both directions of the reciprocal pair) must end up unioned
    into the owning terminal cluster's
    ``relationship_contributed_evidence_ids`` — empirically verified ad
    hoc (A and B land in one shared terminal for this 2-node/1-distinct-
    edge shape) before being committed here."""
    entities = [FixtureEntity(entity_id="A"), FixtureEntity(entity_id="B")]
    relationships = [
        FixtureRelationship(
            relationship_id="r-self",
            source_entity_id="A",
            target_entity_id="A",
            evidence_ids=["ev-selfloop"],
        ),
        FixtureRelationship(
            relationship_id="r-ab",
            source_entity_id="A",
            target_entity_id="B",
            evidence_ids=["ev-ab"],
        ),
        FixtureRelationship(
            relationship_id="r-ba",
            source_entity_id="B",
            target_entity_id="A",
            evidence_ids=["ev-ba"],
        ),
    ]
    fixture = Fixture(
        fixture_id="F-REAL-NATIVE-SELFLOOP-RECIPROCAL",
        description="Self-loop A->A plus reciprocal A->B/B->A, each with distinct evidence.",
        entities=entities,
        relationships=relationships,
    )
    ents, rels = materialize(fixture)

    outcome = build_derived_community_hierarchy(
        ents,
        rels,
        provider=_fresh_provider(),
        run_id="w4-real-native-selfloop-reciprocal-run",
        max_cluster_size=10,
        seed=1,
        **_FIXED_TS_KWARGS,
    )

    assert outcome.status.name == "COMPLETE"

    self_loop_ids = {d.relationship_id for d in outcome.projection.self_loop_diagnostics}
    assert self_loop_ids == {"r-self"}

    terminals = [c for c in outcome.clusters if c.is_terminal]
    assert len(terminals) == 1, (
        "expected A and B to land in one shared terminal for this 2-node/"
        f"1-distinct-edge shape; got {len(terminals)} terminal(s) — real "
        "native output shape drifted from the verified baseline."
    )
    (terminal,) = terminals
    assert set(terminal.member_entity_ids) == {"A", "B"}
    assert set(terminal.relationship_contributed_evidence_ids) == {"ev-selfloop", "ev-ab", "ev-ba"}

    report = validate_derived_hierarchy(outcome)
    assert report.is_valid, report.findings


def test_real_native_node_input_order_does_not_change_terminal_membership_sets() -> None:
    """``fixture_node_permutation`` supplied forward vs. reversed, run
    independently through the real native entrypoint. Per the fixture's
    documented, WEAKER invariant (membership-as-sets, not byte-identical
    row order — row order was found itself not invariant under
    permutation for the real native wrapper): terminal membership sets
    must be identical regardless of entity input order."""
    fwd = fixture_node_permutation(reverse=False)
    rev = fixture_node_permutation(reverse=True)

    def _run(fixture):
        entities, relationships = materialize(fixture)
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=_fresh_provider(),
            run_id=f"w4-real-native-node-perm-{fixture.fixture_id}-run",
            max_cluster_size=10,
            seed=7,
            **_FIXED_TS_KWARGS,
        )
        assert outcome.status.name == "COMPLETE"
        return {frozenset(c.member_entity_ids) for c in outcome.clusters if c.is_terminal}

    fwd_sets = _run(fwd)
    rev_sets = _run(rev)
    expected_sets = {frozenset(s) for s in fwd.expected["connected_components_as_sets"]}
    assert fwd_sets == rev_sets == expected_sets


def test_real_native_relationship_input_order_does_not_change_terminal_membership_sets() -> None:
    """``fixture_relationship_permutation`` supplied forward vs. reversed,
    run independently through the real native entrypoint. Same
    membership-as-sets invariant as the node-permutation test above, but
    varying relationship row order instead of entity row order."""
    fwd = fixture_relationship_permutation(reverse=False)
    rev = fixture_relationship_permutation(reverse=True)

    def _run(fixture):
        entities, relationships = materialize(fixture)
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=_fresh_provider(),
            run_id=f"w4-real-native-rel-perm-{fixture.fixture_id}-run",
            max_cluster_size=10,
            seed=7,
            **_FIXED_TS_KWARGS,
        )
        assert outcome.status.name == "COMPLETE"
        return {frozenset(c.member_entity_ids) for c in outcome.clusters if c.is_terminal}

    fwd_sets = _run(fwd)
    rev_sets = _run(rev)
    expected_sets = {frozenset(s) for s in fwd.expected["connected_components_as_sets"]}
    assert fwd_sets == rev_sets == expected_sets


@pytest.mark.parametrize(
    "fixture_builder",
    [fixture_identical_labels, fixture_long_labels, fixture_unicode_labels],
    ids=["identical", "long", "unicode"],
)
def test_real_native_colliding_labels_still_mint_distinct_cluster_ids(fixture_builder) -> None:
    """``fixture_identical_labels``/``fixture_long_labels``/
    ``fixture_unicode_labels`` (identical, >80-char-truncation-colliding,
    and NFC/NFD-colliding display names respectively) run through the
    real native/actual-core entrypoint. These fixtures were originally
    written as ``known_bug=True`` characterizations of the legacy
    ``graph/community.py:_cluster_id`` label-hashing bug; v2 identity
    (``community_identity.compute_community_identity``) is documented as
    NEVER derived from display labels, so against the actual v2 build
    this is a POSITIVE control, not a bug reproduction: N disjoint
    singleton members sharing/colliding on label text must still mint N
    distinct terminal cluster IDs, keyed off membership alone."""
    fixture = fixture_builder()
    entities, relationships = materialize(fixture)

    outcome = build_derived_community_hierarchy(
        entities,
        relationships,
        provider=_fresh_provider(),
        run_id=f"w4-real-native-labels-{fixture.fixture_id}-run",
        max_cluster_size=10,
        seed=1,
        **_FIXED_TS_KWARGS,
    )

    assert outcome.status.name == "INSUFFICIENT"
    terminals = [c for c in outcome.clusters if c.is_terminal]
    assert len(terminals) == fixture.expected["unique_terminal_cluster_ids"]
    unique_cluster_ids = {c.cluster_id for c in terminals}
    assert len(unique_cluster_ids) == fixture.expected["unique_terminal_cluster_ids"]

    report = validate_derived_hierarchy(outcome)
    assert report.is_valid, report.findings
