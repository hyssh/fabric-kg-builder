"""W4 final acceptance tests for the v2 derived-community-hierarchy core
(``fabric_kg_builder.graph.community_hierarchy_v2`` and friends), against
the parent-corrected build applied directly to this worktree.

Status (per round-7 directive): this file supersedes
``test_actual_core_snapshot_acceptance.py`` (round-5, DELETED from the
shippable patch along with its ``actual_core_overlay.py`` cross-session-path
helper). Every scenario below is an ORDINARY pass/fail assertion of
INTENDED behavior against the corrected core -- not a "defect exists"
assertion. The round-5 file's 13 defect-demonstrating scenarios were each
individually re-verified against this corrected build (see the W4 round-6
report in session ``files/``) and are reproduced here, reframed as the
behavior they now exhibit, plus 7 NEW scenarios required by round-6
(cross-community attribution via a scripted partition cut,
ancestor-evidence union, missing-membership-target, empty-provider-output,
domain-permutation, delimiter-ambiguity, and evidence-sidecar validation).

Imports are DIRECT package imports only (no cross-session overlay, no
hardcoded other-worktree path). Only test-shipped fakes are used as the
community-partition provider:
``community_provider_testing.FixedPartitionProvider`` (caller-scripted
canned partition rows -- used for every scenario that needs a specific,
adversarial, or cross-community-cut shape) and
``DeterministicConnectedComponentsProvider`` (deterministic connected-
components fake -- used only where the exact partition shape does not
matter). Neither is W4's own independently-implemented reference Leiden
algorithm (``fake_provider_contract.py`` remains a separate, already-
accepted temporary QA aid for a different purpose and is intentionally
NOT used or extended here, per round-5/round-6 directives).

Status-vs-exception entrypoint choice, per scenario (matches what was
empirically observed; see technical notes in the round-6/7 reports):
- Invalid INPUT (projection-layer errors: duplicate ids within one
  relationship-list over the same pair, etc.) is asserted via
  ``build_derived_community_hierarchy`` + ``pytest.raises(...)`` directly
  against ``community_projection.build_canonical_projection``, since that
  is the layer that actually raises first.
- Everything else (provider-output conservation, config errors, structural
  validation, successful COMPLETE outcomes) is asserted via the status-
  coded ``build_derived_community_hierarchy_safe`` entrypoint, matching
  how an orchestration/CLI caller would consume this API.
"""

from __future__ import annotations

import re

import pytest

from fabric_kg_builder.graph import community_contracts as contracts
from fabric_kg_builder.graph import community_hierarchy_v2 as hierarchy_v2
from fabric_kg_builder.graph import community_identity as identity
from fabric_kg_builder.graph import community_projection as projection_mod
from fabric_kg_builder.graph import community_validation as validation
from fabric_kg_builder.graph.community_provider_testing import FixedPartitionProvider
from fabric_kg_builder.model.ids import content_hash

from tests.qa_w4_leiden_hierarchy.fixtures import (
    FixtureEntity,
    FixtureRelationship,
    to_entity_row,
    to_relationship_row,
)

NativePartitionEntry = contracts.NativePartitionEntry
HierarchyStatus = contracts.HierarchyStatus


# ---------------------------------------------------------------------------
# Shared helpers (same adapters used by the rest of this QA workstream; see
# fixtures.py for why raw ``EntityRow(...)``/``RelationshipRow(...)``
# construction is NOT used directly in this file).
# ---------------------------------------------------------------------------


def _entity(entity_id: str, *, domain_hash: str | None = None, evidence_ids: list[str] | None = None):
    row = to_entity_row(
        FixtureEntity(entity_id=entity_id, display_name=entity_id, evidence_ids=evidence_ids)
    )
    if domain_hash is not None:
        row = row.model_copy(update={"domain_hash": domain_hash})
    return row


def _relationship(
    rel_id: str,
    src: str,
    tgt: str,
    rel_type: str = "related_to",
    evidence_ids: list[str] | None = None,
):
    return to_relationship_row(
        FixtureRelationship(
            relationship_id=rel_id,
            source_entity_id=src,
            target_entity_id=tgt,
            relationship_type=rel_type,
            evidence_ids=evidence_ids,
        )
    )


def _entry(node_id, cluster_id, parent_cluster_id, level, is_final_cluster):
    return NativePartitionEntry(
        node_id=node_id,
        cluster_id=cluster_id,
        parent_cluster_id=parent_cluster_id,
        level=level,
        is_final_cluster=is_final_cluster,
    )


def _build(
    entities,
    relationships,
    provider,
    *,
    max_cluster_size=1_000,
    seed=0,
    policy_id="qa-w4-policy",
    run_id="qa-w4-run",
    config_id="qa-w4-config",
    entity_evidence=None,
    relationship_evidence=None,
):
    return hierarchy_v2.build_derived_community_hierarchy_safe(
        entities=entities,
        relationships=relationships,
        provider=provider,
        policy_id=policy_id,
        run_id=run_id,
        config_id=config_id,
        max_cluster_size=max_cluster_size,
        seed=seed,
        entity_evidence=entity_evidence,
        relationship_evidence=relationship_evidence,
    )


# ===========================================================================
# PART 1: the 13 former round-5 defect scenarios, reproduced as ordinary
# pass/fail acceptance of INTENDED (corrected) behavior.
# ===========================================================================


def test_hierarchy_status_enum_values():
    assert HierarchyStatus.COMPLETE.value == "complete"
    assert HierarchyStatus.EMPTY.value == "empty"
    assert HierarchyStatus.INSUFFICIENT.value == "insufficient"
    assert HierarchyStatus.NATIVE_UNAVAILABLE.value == "native_unavailable"
    assert HierarchyStatus.FAILED.value == "failed"


def test_empty_entities_with_nonempty_relationships_is_invalid_and_fails():
    """EMPTY with zero entities AND a nonempty (dangling) relationships list
    is an INVALID input combination, not a positive confirmation of the
    EMPTY path -- the corrected core must FAIL it, not report a bare
    EMPTY/INSUFFICIENT success with the dangling relationships silently
    discarded."""
    relationships = [_relationship("R1", "A", "B")]
    outcome = _build([], relationships, FixedPartitionProvider([]))

    assert outcome.status == HierarchyStatus.FAILED
    assert outcome.reason is not None
    assert outcome.clusters == []
    assert outcome.memberships == []


def test_unknown_endpoint_relationship_is_rejected_with_explicit_diagnostic():
    entities = [_entity("A")]
    relationships = [_relationship("R-GHOST", "A", "UNKNOWN_ENTITY")]

    with pytest.raises(projection_mod.CanonicalProjectionError) as excinfo:
        projection_mod.build_canonical_projection(entities=entities, relationships=relationships)

    assert contracts.REASON_UNKNOWN_RELATIONSHIP_ENDPOINT in str(excinfo.value) or (
        "unknown" in str(excinfo.value).lower() and "endpoint" in str(excinfo.value).lower()
    )

    outcome = _build(entities, relationships, FixedPartitionProvider([]))
    assert outcome.status == HierarchyStatus.FAILED


def test_duplicate_relationship_id_same_pair_raises_at_projection_layer():
    entities = [_entity("A"), _entity("B")]
    relationships = [
        _relationship("R-DUP", "A", "B", rel_type="knows"),
        _relationship("R-DUP", "B", "A", rel_type="knows"),
    ]

    with pytest.raises(projection_mod.CanonicalProjectionError) as excinfo:
        projection_mod.build_canonical_projection(entities=entities, relationships=relationships)
    message = str(excinfo.value).lower()
    assert "duplicate" in message and "relationship_id" in message and "r-dup" in message


def test_duplicate_relationship_id_across_different_pairs_fails_not_last_wins():
    """Two DIFFERENT relationship rows sharing one relationship_id, over two
    DIFFERENT pairs, must now be rejected explicitly rather than silently
    resolved by a last-wins dict comprehension that reported the wrong
    (other) relationship's type for one of the pairs."""
    entities = [_entity("A"), _entity("B"), _entity("C")]
    relationships = [
        _relationship("R-DUP", "A", "B", rel_type="knows"),
        _relationship("R-DUP", "A", "C", rel_type="works_with"),
    ]
    entries = [
        _entry("A", 0, None, 0, False),
        _entry("A", 1, 0, 1, True),
        _entry("B", 2, 0, 1, True),
        _entry("C", 3, 0, 1, True),
    ]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries))

    assert outcome.status == HierarchyStatus.FAILED
    message = str(outcome.reason).lower()
    assert "duplicate" in message and "relationship_id" in message and "r-dup" in message


def test_missing_native_entry_is_rejected_as_non_conserving():
    """A node present in the projection's input graph but absent from the
    provider's returned entries must FAIL with an explicit node-
    conservation violation -- not silently succeed with the entity
    dropped and the validator reporting zero findings."""
    entities = [_entity("A"), _entity("B"), _entity("C")]
    relationships = [_relationship("R1", "A", "B"), _relationship("R2", "A", "C")]
    entries = [
        _entry("A", 0, None, 0, False),
        _entry("A", 1, 0, 1, True),
        _entry("B", 2, 0, 1, True),
        # C intentionally omitted.
    ]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries))

    assert outcome.status == HierarchyStatus.FAILED
    assert "conserve" in outcome.reason.lower()
    assert "C" in outcome.reason


def test_unknown_native_node_id_is_rejected_not_silently_accepted():
    entities = [_entity("A"), _entity("B")]
    relationships = [_relationship("R1", "A", "B")]
    entries = [
        _entry("A", 0, None, 0, False),
        _entry("A", 1, 0, 1, True),
        _entry("B", 1, 0, 1, True),
        _entry("GHOST", 1, 0, 1, True),  # never part of entities/relationships
    ]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries))

    assert outcome.status == HierarchyStatus.FAILED
    assert "unknown" in outcome.reason.lower()
    assert "GHOST" in outcome.reason


def test_parent_cycle_in_provider_output_is_rejected_positive_control():
    """Positive control, unchanged by the fix: a genuine parent-link cycle
    with no reachable root is -- and must remain -- rejected."""
    entities = [_entity("A"), _entity("B")]
    relationships = [_relationship("R1", "A", "B")]
    entries = [
        _entry("A", 1, 2, 1, True),
        _entry("B", 2, 1, 1, True),
    ]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries))

    assert outcome.status == HierarchyStatus.FAILED
    assert outcome.reason is not None and "root" in outcome.reason.lower()


def test_backward_lexicographic_relationship_diagnostic_preserves_original_direction():
    """A directed relationship whose source sorts AFTER its target
    lexicographically must have its diagnostic's source/target reflect the
    ORIGINAL RelationshipRow direction, not the canonical sorted pair."""
    entities = [_entity("zz_node"), _entity("aa_node")]
    relationships = [_relationship("R-BACK", "zz_node", "aa_node", rel_type="depends_on")]
    entries = [
        _entry("zz_node", 0, None, 0, False),
        _entry("zz_node", 1, 0, 1, True),
        _entry("aa_node", 2, 0, 1, True),
    ]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries))

    assert outcome.status == HierarchyStatus.COMPLETE
    assert len(outcome.inter_community_relationship_diagnostics) == 1
    diag = outcome.inter_community_relationship_diagnostics[0]
    assert (diag.source_entity_id, diag.target_entity_id) == ("zz_node", "aa_node")


def test_unbalanced_depth_shape_terminal_height0_parent_sum_native_depth_independent():
    """REQUIRED positive control (not a defect): terminal structural_height
    is always 0; a parent's structural_height is 1 + max(child heights);
    native_depth is stored verbatim as the provider's root-first ``level``
    with NO global-max-depth subtraction anywhere, independent of
    structural_height. Uses a genuinely unbalanced tree (C terminal at a
    SHALLOWER native depth than A/B's branch) so the two notions of
    "depth" are distinguishable."""
    entities = [_entity(n) for n in ("A", "B", "C")]
    relationships = [_relationship("R1", "A", "B"), _relationship("R2", "B", "C")]
    entries = [
        _entry("C", 0, None, 0, False),
        _entry("A", 10, 0, 1, False),
        _entry("A", 1, 10, 2, True),
        _entry("B", 2, 10, 2, True),
        _entry("C", 3, 0, 1, True),
    ]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries))
    assert outcome.status == HierarchyStatus.COMPLETE

    by_members = {frozenset(c.member_entity_ids): c for c in outcome.clusters}
    term_a = by_members[frozenset({"A"})]
    term_b = by_members[frozenset({"B"})]
    term_c = by_members[frozenset({"C"})]
    mid_ab = by_members[frozenset({"A", "B"})]
    root = by_members[frozenset({"A", "B", "C"})]

    assert term_a.structural_height == 0
    assert term_b.structural_height == 0
    assert term_c.structural_height == 0
    assert mid_ab.structural_height == 1  # 1 + max(0, 0)
    assert root.structural_height == 2  # 1 + max(mid_ab=1, term_c=0)

    assert term_a.native_depth == 2
    assert term_b.native_depth == 2
    assert term_c.native_depth == 1  # shallower native depth, same structural height as A/B
    assert mid_ab.native_depth == 1
    assert root.native_depth == 0


def test_malformed_parent_native_depth_does_not_crash_child_lineage_check():
    """Regression for a blocker independently reproduced by W4 against a
    prior W2 delta (``ee9f31``): a non-``None`` malformed ``native_depth``
    on a PARENT cluster (e.g. the provider supplying a non-int ``level``)
    must never reach the ``parent.native_depth + 1`` lineage arithmetic in
    ``_validate_native_depth`` and crash the public build entrypoint with a
    raw ``TypeError``. The only prior guard (``parent.native_depth is
    None: continue``) did not cover this non-None-but-invalid case.

    Exercised end-to-end through the actual public ``build_*_safe``
    entrypoint with a scripted ``FixedPartitionProvider`` whose root-level
    entries carry a malformed (non-int) ``level`` value -- not a hand-built
    ``DerivedHierarchyOutcome`` -- so this is proof against the real
    provider -> tree-assembly -> validation pipeline, not just the
    validator function in isolation.

    A follow-up W2 delta (checksummed ``7de729a...``, applied atop
    ``ee9f31``) adds the missing guard; this test is written as an ordinary
    (non-xfail) pass/fail assertion of the now-corrected intended behavior,
    consistent with W4's independently-reproduced confirmation that the
    crash no longer occurs against the corrected source. This does not
    constitute W4's acceptance of the broader delta -- see the parent
    report for full disposition."""
    entities = [_entity("A"), _entity("B")]
    relationships = [_relationship("R1", "A", "B")]
    entries = [
        _entry("A", "root", None, "bad", False),
        _entry("B", "root", None, "bad", False),
        _entry("A", "leafA", "root", 1, True),
        _entry("B", "leafB", "root", 1, True),
    ]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries))

    # Must not raise -- this is the core regression assertion. A malformed
    # provider-supplied level/native_depth is an input-shape defect, so the
    # safe entrypoint must surface it as FAILED, never propagate the
    # TypeError.
    assert outcome.status == HierarchyStatus.FAILED
    assert outcome.reason is not None
    assert "native_depth" in outcome.reason and "bad" in outcome.reason


def test_oversized_terminal_flagged_not_rejected_positive_control():
    """REQUIRED positive control: an oversized terminal cluster is flagged
    (``is_oversized`` / ``oversized_terminal_cluster_ids``) but never
    rejected or split by this layer -- Leiden's own ``max_cluster_size`` is
    a split TRIGGER for the provider, not a hard cap enforced here."""
    entities = [_entity(n) for n in ("A", "B", "C", "D")]
    relationships = [
        _relationship("R1", "A", "B"),
        _relationship("R2", "B", "C"),
        _relationship("R3", "C", "D"),
    ]
    entries = [_entry(n, 1, None, 0, True) for n in ("A", "B", "C", "D")]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries), max_cluster_size=2)

    assert outcome.status == HierarchyStatus.COMPLETE
    assert len(outcome.clusters) == 1
    cluster = outcome.clusters[0]
    assert cluster.is_oversized is True
    assert set(cluster.member_entity_ids) == {"A", "B", "C", "D"}
    assert cluster.cluster_id in outcome.oversized_terminal_cluster_ids


_MAKE_ID_PATTERN = re.compile(r"^[a-z]+:[0-9a-f]{32}$")
_FULL_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def test_cluster_id_remains_prefixed32_execution_fingerprint_is_now_full_sha256():
    """Per round-7 directive: cluster_id may remain a prefixed, truncated
    (32-hex) ``make_id``-style identifier -- that is NOT the integration
    defect. The actual defect was ``execution_fingerprint`` claiming to be
    a full SHA256 receipt while actually being the SAME truncated/prefixed
    format; the corrected core must emit a bare, untruncated 64-hex-char
    SHA256 digest for ``execution_fingerprint``."""
    cluster_id = identity.compute_community_identity(
        domain_hash=None,
        provider_id="p",
        provider_version="1",
        policy_id="pol",
        member_entity_ids=("A", "B"),
        structural_lineage=(),
    )
    fingerprint = identity.compute_execution_fingerprint(
        input_graph_hash="deadbeef",
        config_id="cfg",
        provider_id="p",
        provider_version="1",
        seed=0,
        policy_id="pol",
        max_cluster_size=10,
    )

    # Community IDs may remain prefixed/truncated -- NOT a defect.
    assert _MAKE_ID_PATTERN.match(cluster_id), cluster_id

    # execution_fingerprint MUST now be a bare, full, untruncated SHA256
    # hex digest -- matching the documented fullSha256 receipt contract.
    assert _FULL_SHA256_PATTERN.match(fingerprint), (
        f"execution_fingerprint {fingerprint!r} is not a bare full SHA256 "
        "hex digest; it must not reuse the truncated/prefixed make_id format."
    )
    assert ":" not in fingerprint

    full_digest = content_hash("some-content")
    assert _FULL_SHA256_PATTERN.match(full_digest)
    assert len(fingerprint) == len(full_digest) == 64


def test_input_graph_hash_binds_relationship_type():
    """The projection's input_graph_hash (folded into execution_fingerprint)
    must distinguish two inputs that differ ONLY in relationship_type,
    with source/target orientation held fixed -- separately parameterized
    from the direction mutation below so each dimension is independently
    proven, per round-9 feedback that the prior combined test changed
    type AND direction together."""
    entities = [_entity("A"), _entity("B")]
    rels_knows = [_relationship("R1", "A", "B", rel_type="knows")]
    rels_works_with = [_relationship("R1", "A", "B", rel_type="works_with")]

    proj_a = projection_mod.build_canonical_projection(entities=entities, relationships=rels_knows)
    proj_b = projection_mod.build_canonical_projection(entities=entities, relationships=rels_works_with)

    assert proj_a.input_graph_hash != proj_b.input_graph_hash, (
        "input_graph_hash must bind relationship_type; two inputs differing "
        "only in type (direction held fixed) hashed identically."
    )


def test_input_graph_hash_binds_original_direction():
    """The projection's input_graph_hash must distinguish two inputs that
    differ ONLY in original directed source/target orientation, with
    relationship_type held fixed, even though both produce the identical
    UNDIRECTED edge set -- this is REQUIRED so the hash is a faithful
    receipt of the actual directed input, not just its undirected shadow.
    Separately parameterized from the type mutation above."""
    entities = [_entity("A"), _entity("B")]
    rels_forward = [_relationship("R1", "A", "B", rel_type="knows")]
    rels_backward = [_relationship("R1", "B", "A", rel_type="knows")]

    proj_a = projection_mod.build_canonical_projection(entities=entities, relationships=rels_forward)
    proj_b = projection_mod.build_canonical_projection(entities=entities, relationships=rels_backward)

    assert proj_a.input_graph_hash != proj_b.input_graph_hash, (
        "input_graph_hash must bind original direction; two inputs "
        "differing only in source/target orientation (type held fixed) "
        "hashed identically."
    )


# ===========================================================================
# PART 2: the 7 round-6 NEW required additions.
# ===========================================================================


def test_cross_community_attribution_via_scripted_partition_cut():
    """Cross-community relationship attribution must be exercised via a
    SCRIPTED partition cut (not by assuming Leiden/the provider must chunk
    by max_cluster_size -- that is only a split trigger). A,C in one
    terminal cluster; B,D in another; relationship A<->B crosses the cut.
    """
    entities = [_entity(n) for n in ("A", "B", "C", "D")]
    relationships = [
        _relationship("R-AC", "A", "C", rel_type="related_to"),
        _relationship("R-BD", "B", "D", rel_type="related_to"),
        _relationship("R-CROSS", "A", "B", rel_type="depends_on"),
    ]
    entries = [
        _entry("A", 0, None, 0, False),
        _entry("A", 1, 0, 1, True),
        _entry("C", 1, 0, 1, True),
        _entry("B", 2, 0, 1, True),
        _entry("D", 2, 0, 1, True),
    ]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries))

    assert outcome.status == HierarchyStatus.COMPLETE
    assert len(outcome.inter_community_relationship_diagnostics) == 1
    diag = outcome.inter_community_relationship_diagnostics[0]
    assert diag.relationship_id == "R-CROSS"
    assert diag.relationship_type == "depends_on"
    assert {diag.source_entity_id, diag.target_entity_id} == {"A", "B"}
    assert diag.source_cluster_id != diag.target_cluster_id
    assert diag.reason == contracts.REASON_INTER_COMMUNITY_RELATIONSHIP_EVIDENCE_DUAL_ATTRIBUTION


def test_ancestor_evidence_union_propagates_bottom_up():
    """3-node chain A-B-C with per-entity and per-relationship evidence:
    each terminal gets its own entity_evidence plus any incident
    relationship_evidence; the root ancestor's evidence_ids is the FULL
    UNION of every descendant terminal's evidence_ids."""
    entities = [_entity(n) for n in ("A", "B", "C")]
    relationships = [
        _relationship("R-AB", "A", "B"),
        _relationship("R-BC", "B", "C"),
    ]
    entries = [
        _entry("A", 0, None, 0, False),
        _entry("A", 1, 0, 1, True),
        _entry("B", 2, 0, 1, True),
        _entry("C", 3, 0, 1, True),
    ]
    outcome = _build(
        entities,
        relationships,
        FixedPartitionProvider(entries),
        entity_evidence={"A": ["ev-a1"], "B": ["ev-b1"], "C": ["ev-c1"]},
        relationship_evidence={"R-AB": ["ev-r1"], "R-BC": ["ev-r2"]},
    )
    assert outcome.status == HierarchyStatus.COMPLETE

    by_members = {frozenset(c.member_entity_ids): c for c in outcome.clusters}
    term_a = by_members[frozenset({"A"})]
    term_b = by_members[frozenset({"B"})]
    term_c = by_members[frozenset({"C"})]
    root = by_members[frozenset({"A", "B", "C"})]

    assert set(term_a.evidence_ids) == {"ev-a1", "ev-r1"}
    assert set(term_b.evidence_ids) == {"ev-b1", "ev-r1", "ev-r2"}
    assert set(term_c.evidence_ids) == {"ev-c1", "ev-r2"}
    assert set(root.evidence_ids) == {"ev-a1", "ev-b1", "ev-c1", "ev-r1", "ev-r2"}


def test_missing_membership_target_entity_fully_absent_from_provider_output():
    """A connected entity (part of a relationship, so not short-circuited
    by the unrelated INSUFFICIENT path) that has ZERO rows anywhere in the
    provider output must FAIL with an explicit node-conservation
    violation."""
    entities = [_entity("A"), _entity("B"), _entity("C")]
    relationships = [_relationship("R1", "A", "B"), _relationship("R2", "B", "C")]
    entries = [
        _entry("A", 1, None, 0, True),
        _entry("B", 1, None, 0, True),
        # C has zero partition entries at any level.
    ]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries))

    assert outcome.status == HierarchyStatus.FAILED
    assert "conserve" in outcome.reason.lower()
    assert "C" in outcome.reason


def test_empty_provider_output_with_nonempty_connected_entities_fails():
    """A provider that returns NO partition entries at all, when nonempty
    connected entities were submitted, must FAIL with an explicit
    conservation violation naming the submitted-entity count."""
    entities = [_entity("A"), _entity("B")]
    relationships = [_relationship("R1", "A", "B")]
    outcome = _build(entities, relationships, FixedPartitionProvider([]))

    assert outcome.status == HierarchyStatus.FAILED
    assert "no partition entries" in outcome.reason.lower()
    assert "2" in outcome.reason


def test_domain_permutation_mixed_domain_input_rejected_order_independent():
    """Mixed-domain input (entities spanning >1 distinct domain_hash) must
    be REJECTED explicitly -- not arbitrarily resolved to entities[0]'s
    domain (which would make the result sensitive to input list order).
    Confirmed order-independent: the same rejection and reason fire
    whether the mixed-domain entities/relationships are supplied in
    forward or fully-reversed list order."""
    entities = [_entity("A", domain_hash="dom1"), _entity("B", domain_hash="dom2")]
    relationships = [_relationship("R1", "A", "B")]
    entries = [_entry("A", 1, None, 0, True), _entry("B", 1, None, 0, True)]
    provider = FixedPartitionProvider(entries)

    forward = _build(entities, relationships, provider)
    reversed_ = _build(list(reversed(entities)), list(reversed(relationships)), provider)

    assert forward.status == HierarchyStatus.FAILED
    assert "domain" in forward.reason.lower()
    assert reversed_.status == HierarchyStatus.FAILED
    assert reversed_.reason == forward.reason


def test_delimiter_ambiguity_colon_containing_ids_do_not_collide():
    """Entity ids containing a colon delimiter character -- including one
    with a DOUBLED colon -- must not collide with, or be misparsed
    relative to, other entity ids."""
    entities = [_entity("ns:A"), _entity("ns:B"), _entity("ns::C")]
    relationships = [
        _relationship("R1", "ns:A", "ns:B"),
        _relationship("R2", "ns:B", "ns::C"),
    ]
    entries = [
        _entry("ns:A", 0, None, 0, False),
        _entry("ns:A", 1, 0, 1, True),
        _entry("ns:B", 2, 0, 1, True),
        _entry("ns::C", 3, 0, 1, True),
    ]
    outcome = _build(entities, relationships, FixedPartitionProvider(entries))

    assert outcome.status == HierarchyStatus.COMPLETE
    all_members = {eid for c in outcome.clusters for eid in c.member_entity_ids}
    assert all_members == {"ns:A", "ns:B", "ns::C"}
    assert len(all_members) == 3  # no delimiter-based collision/merge


class TestSidecarScenarios:
    """Evidence sidecar validation (``_validate_evidence_sidecar`` via the
    public entrypoints' ``entity_evidence``/``relationship_evidence``
    parameters). Per the round-7 "sidecar decision": missing canonical
    keys are allowed (authoritative empty evidence, not an error); unknown
    entity/relationship keys and wrong-shaped values (including a bare
    string passed where a sequence of strings is required) must fail
    explicitly. External evidence-corpus authority remains W3's sealed
    source; this only tests the sidecar's own structural contract."""

    def test_missing_canonical_key_is_allowed_as_authoritative_empty(self):
        entities = [_entity("A"), _entity("B")]
        relationships = [_relationship("R1", "A", "B")]
        entries = [_entry("A", 1, None, 0, True), _entry("B", 1, None, 0, True)]
        # entity_evidence omits BOTH "A" and "B" entirely -- allowed.
        outcome = _build(
            entities,
            relationships,
            FixedPartitionProvider(entries),
            entity_evidence={},
            relationship_evidence={},
        )
        assert outcome.status == HierarchyStatus.COMPLETE
        for cluster in outcome.clusters:
            assert cluster.evidence_ids == ()

    def test_unknown_entity_key_in_sidecar_fails_explicitly(self):
        entities = [_entity("A"), _entity("B")]
        relationships = [_relationship("R1", "A", "B")]
        entries = [_entry("A", 1, None, 0, True), _entry("B", 1, None, 0, True)]
        outcome = _build(
            entities,
            relationships,
            FixedPartitionProvider(entries),
            entity_evidence={"UNKNOWN_ENTITY": ["ev-1"]},
        )
        assert outcome.status == HierarchyStatus.FAILED
        assert "unknown" in outcome.reason.lower()
        assert "UNKNOWN_ENTITY" in outcome.reason

    def test_string_as_sequence_value_fails_explicitly(self):
        """A bare string value (itself iterable character-by-character in
        Python) must be rejected, not silently accepted as a one-item-per-
        character "sequence of strings"."""
        entities = [_entity("A"), _entity("B")]
        relationships = [_relationship("R1", "A", "B")]
        entries = [_entry("A", 1, None, 0, True), _entry("B", 1, None, 0, True)]
        outcome = _build(
            entities,
            relationships,
            FixedPartitionProvider(entries),
            entity_evidence={"A": "not-a-list"},
        )
        assert outcome.status == HierarchyStatus.FAILED
        assert "sequence" in outcome.reason.lower()

    def test_sidecar_entirely_omitted_falls_back_to_source_row_evidence(self):
        """``entity_evidence=None``/``relationship_evidence=None`` (the
        default, meaning the sidecar parameter itself is omitted, not an
        empty mapping) must NOT be treated as "no evidence anywhere" --
        it must fall back to each row's own ``evidence_ids`` field, per
        the documented sidecar-omitted contract."""
        entities = [
            _entity("A", evidence_ids=["ev-entity-a"]),
            _entity("B"),
        ]
        relationships = [_relationship("R1", "A", "B", evidence_ids=["ev-rel-r1"])]
        entries = [_entry("A", 1, None, 0, True), _entry("B", 1, None, 0, True)]

        outcome = _build(
            entities,
            relationships,
            FixedPartitionProvider(entries),
            entity_evidence=None,
            relationship_evidence=None,
        )
        assert outcome.status == HierarchyStatus.COMPLETE
        (cluster,) = outcome.clusters
        assert set(cluster.evidence_ids) == {"ev-entity-a", "ev-rel-r1"}

    def test_non_sequence_value_type_fails_explicitly(self):
        """A non-sequence, non-string scalar value (an int) must be
        rejected with the same "must be a sequence" diagnostic as the
        bare-string case -- distinct from the non-string-ELEMENT case
        below, where the value IS a sequence but one of its items is not
        a string."""
        entities = [_entity("A"), _entity("B")]
        relationships = [_relationship("R1", "A", "B")]
        entries = [_entry("A", 1, None, 0, True), _entry("B", 1, None, 0, True)]
        outcome = _build(
            entities,
            relationships,
            FixedPartitionProvider(entries),
            entity_evidence={"A": 123},
        )
        assert outcome.status == HierarchyStatus.FAILED
        assert "sequence" in outcome.reason.lower()

    def test_non_string_elements_in_otherwise_valid_sequence_fail_explicitly(self):
        """A list value (a genuine sequence, not a bare string) containing
        a non-string element must still be rejected explicitly, distinct
        from (and not silently coerced like) the non-sequence-value case
        above."""
        entities = [_entity("A"), _entity("B")]
        relationships = [_relationship("R1", "A", "B")]
        entries = [_entry("A", 1, None, 0, True), _entry("B", 1, None, 0, True)]
        outcome = _build(
            entities,
            relationships,
            FixedPartitionProvider(entries),
            relationship_evidence={"R1": ["ev-ok", 42]},
        )
        assert outcome.status == HierarchyStatus.FAILED
        assert "string" in outcome.reason.lower()
