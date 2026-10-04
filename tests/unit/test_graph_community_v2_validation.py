"""Tests for graph/community_validation.py — v2 derived-hierarchy structural
validators. Constructs ``DerivedHierarchyOutcome``/``DerivedClusterRow``/
``DerivedMembershipRow`` directly (rather than going through a provider) so
each validator can be exercised precisely, including deliberately-broken
inputs that a correct implementation should never itself produce but which
the validator must still catch.
"""

from __future__ import annotations

import pytest

from fabric_kg_builder.graph.community_contracts import (
    DerivedClusterRow,
    DerivedHierarchyOutcome,
    DerivedMembershipRow,
    HierarchyStatus,
)
from fabric_kg_builder.graph.community_hierarchy_v2 import build_derived_community_hierarchy
from fabric_kg_builder.graph.community_identity import compute_community_identity
from fabric_kg_builder.graph.community_provider_testing import (
    DeterministicConnectedComponentsProvider,
)
from fabric_kg_builder.graph.community_validation import (
    ValidationFinding,
    ValidationReport,
    assert_valid_derived_hierarchy,
    validate_derived_hierarchy,
)

pytestmark = pytest.mark.unit

DOMAIN_HASH = None
PROVIDER_ID = "stub_connected_components"
PROVIDER_VERSION = "test-fake-1"
POLICY_ID = "policy-a"
RUN_ID = "run-1"


def _terminal(
    member_ids: tuple[str, ...],
    *,
    parent: str | None,
    evidence_ids: tuple[str, ...] = (),
    relationship_contributed_evidence_ids: tuple[str, ...] = (),
) -> DerivedClusterRow:
    cluster_id = compute_community_identity(
        domain_hash=DOMAIN_HASH,
        provider_id=PROVIDER_ID,
        provider_version=PROVIDER_VERSION,
        policy_id=POLICY_ID,
        member_entity_ids=member_ids,
        structural_lineage=(),
    )
    return DerivedClusterRow(
        cluster_id=cluster_id,
        hierarchy_version="2.0-derived-leiden",
        parent_cluster_id=parent,
        structural_height=0,
        native_depth=None if len(member_ids) == 1 else 0,
        is_terminal=True,
        is_singleton_isolate=len(member_ids) == 1,
        is_oversized=False,
        member_entity_ids=member_ids,
        evidence_ids=evidence_ids,
        provider_id=PROVIDER_ID,
        provider_version=PROVIDER_VERSION,
        policy_id=POLICY_ID,
        domain_hash=DOMAIN_HASH,
        run_id=RUN_ID,
        execution_fingerprint="fp-1",
        relationship_contributed_evidence_ids=relationship_contributed_evidence_ids,
    )


def _internal(
    children: tuple[DerivedClusterRow, ...],
    *,
    parent: str | None,
) -> DerivedClusterRow:
    members = tuple(sorted({m for c in children for m in c.member_entity_ids}))
    evidence = tuple(sorted({e for c in children for e in c.evidence_ids}))
    lineage = tuple(sorted(c.cluster_id for c in children))
    cluster_id = compute_community_identity(
        domain_hash=DOMAIN_HASH,
        provider_id=PROVIDER_ID,
        provider_version=PROVIDER_VERSION,
        policy_id=POLICY_ID,
        member_entity_ids=members,
        structural_lineage=lineage,
    )
    height = 1 + max(c.structural_height for c in children)
    return DerivedClusterRow(
        cluster_id=cluster_id,
        hierarchy_version="2.0-derived-leiden",
        parent_cluster_id=parent,
        structural_height=height,
        native_depth=0,
        is_terminal=False,
        is_singleton_isolate=False,
        is_oversized=False,
        member_entity_ids=members,
        evidence_ids=evidence,
        provider_id=PROVIDER_ID,
        provider_version=PROVIDER_VERSION,
        policy_id=POLICY_ID,
        domain_hash=DOMAIN_HASH,
        run_id=RUN_ID,
        execution_fingerprint="fp-1",
    )


def _memberships(terminals: tuple[DerivedClusterRow, ...]) -> list[DerivedMembershipRow]:
    return [
        DerivedMembershipRow(cluster_id=t.cluster_id, entity_id=m, evidence_ids=())
        for t in terminals
        for m in t.member_entity_ids
    ]


def _valid_two_level_outcome() -> DerivedHierarchyOutcome:
    """A -> {a}, B -> {b, c} as two terminals under one internal parent.

    ``native_depth`` is patched to a coherent root-first lineage (root
    internal cluster at depth 0, its non-isolate terminal child at depth 1)
    to satisfy ``_validate_native_depth``'s parent+1 ancestor-lineage check
    — the ``_terminal``/``_internal`` helpers default to a flat 0 for
    standalone single-cluster fixtures that never form a real tree.
    ``leaf_a`` is a singleton isolate (single member "a"), so its
    ``native_depth`` stays ``None`` — only non-isolate clusters participate
    in the native-depth lineage check."""
    leaf_a = _terminal(("a",), parent=None, evidence_ids=("ev-a",))
    leaf_bc = _terminal(("b", "c"), parent=None, evidence_ids=("ev-b", "ev-c"))
    parent = _internal((leaf_a, leaf_bc), parent=None)
    leaf_a = leaf_a.__class__(**{**leaf_a.__dict__, "parent_cluster_id": parent.cluster_id})
    leaf_bc = leaf_bc.__class__(
        **{**leaf_bc.__dict__, "parent_cluster_id": parent.cluster_id, "native_depth": 1}
    )
    clusters = [leaf_a, leaf_bc, parent]
    memberships = _memberships((leaf_a, leaf_bc))
    return DerivedHierarchyOutcome(
        status=HierarchyStatus.COMPLETE,
        clusters=clusters,
        memberships=memberships,
        provider_id=PROVIDER_ID,
        provider_version=PROVIDER_VERSION,
    )


class TestValidOutcomes:
    def test_empty_outcome_is_valid(self) -> None:
        outcome = DerivedHierarchyOutcome(status=HierarchyStatus.EMPTY, reason="empty_entity_input")
        report = validate_derived_hierarchy(outcome)
        assert report.is_valid
        assert_valid_derived_hierarchy(outcome)

    def test_two_level_tree_is_valid(self) -> None:
        outcome = _valid_two_level_outcome()
        report = validate_derived_hierarchy(outcome)
        assert report.is_valid, report.findings
        assert_valid_derived_hierarchy(outcome)

    def test_real_build_via_stub_provider_is_valid(self) -> None:
        from fabric_kg_builder.model.schemas import EntityRow, RelationshipRow
        from datetime import datetime, timezone
        import hashlib

        def entity(eid: str) -> EntityRow:
            key = hashlib.sha1(eid.encode()).hexdigest()[:16]
            return EntityRow(
                entity_id=eid,
                entity_type="org",
                display_name=eid,
                canonical_key=key,
                content_hash=key,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

        def rel(rid: str, s: str, t: str) -> RelationshipRow:
            key = hashlib.sha1(rid.encode()).hexdigest()[:16]
            return RelationshipRow(
                relationship_id=rid,
                relationship_type="knows",
                source_entity_id=s,
                target_entity_id=t,
                content_hash=key,
                created_at=datetime.now(timezone.utc),
            )

        ids = [f"n{i}" for i in range(6)]
        entities = [entity(i) for i in ids]
        relationships = [rel(f"r{i}", ids[i], ids[i + 1]) for i in range(len(ids) - 1)]
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=DeterministicConnectedComponentsProvider(),
            policy_id=POLICY_ID,
            run_id=RUN_ID,
            max_cluster_size=2,
        )
        report = validate_derived_hierarchy(outcome)
        assert report.is_valid, report.findings


class TestTreeStructureValidator:
    def test_dangling_parent_reference_caught(self) -> None:
        leaf = _terminal(("a",), parent="ghost-parent")
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[leaf],
            memberships=_memberships((leaf,)),
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "dangling_parent_reference" for f in report.findings)

    def test_parent_cycle_caught(self) -> None:
        a = _terminal(("a",), parent="b-id")
        b = _terminal(("b",), parent="a-id")
        a = a.__class__(**{**a.__dict__, "cluster_id": "a-id", "parent_cluster_id": "b-id"})
        b = b.__class__(**{**b.__dict__, "cluster_id": "b-id", "parent_cluster_id": "a-id"})
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[a, b],
            memberships=[],
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "parent_cycle" for f in report.findings)

    def test_terminal_cluster_with_children_caught(self) -> None:
        child = _terminal(("a",), parent="parent-id")
        fake_terminal_with_child = child.__class__(
            **{**child.__dict__, "cluster_id": "parent-id", "is_terminal": True}
        )
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[child, fake_terminal_with_child],
            memberships=[],
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "terminal_cluster_has_children" for f in report.findings)

    def test_terminal_cluster_nonzero_height_caught(self) -> None:
        leaf = _terminal(("a",), parent=None)
        broken = leaf.__class__(**{**leaf.__dict__, "structural_height": 3})
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE, clusters=[broken], memberships=_memberships((broken,))
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "terminal_cluster_nonzero_height" for f in report.findings)

    def test_internal_cluster_with_no_children_caught(self) -> None:
        lone_internal = _internal((_terminal(("a",), parent=None),), parent=None)
        orphan_internal = lone_internal.__class__(
            **{**lone_internal.__dict__, "cluster_id": "orphan-internal"}
        )
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE, clusters=[orphan_internal], memberships=[]
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "internal_cluster_has_no_children" for f in report.findings)

    def test_incorrect_structural_height_caught(self) -> None:
        leaf = _terminal(("a",), parent=None)
        parent = _internal((leaf,), parent=None)
        leaf = leaf.__class__(**{**leaf.__dict__, "parent_cluster_id": parent.cluster_id})
        broken_parent = parent.__class__(**{**parent.__dict__, "structural_height": 99})
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[leaf, broken_parent],
            memberships=_memberships((leaf,)),
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "incorrect_structural_height" for f in report.findings)


class TestNativeDepthValidator:
    """Regression coverage for ``_validate_native_depth``'s full ancestor-
    lineage check: non-negative int type, root depth == 0, and each
    non-root cluster's depth == parent.native_depth + 1."""

    def _two_level_tree(self) -> tuple[DerivedClusterRow, DerivedClusterRow]:
        # Two members ("a", "x") so this terminal is NOT a singleton
        # isolate — isolates always carry native_depth=None and are
        # exempt from the lineage check (covered separately below).
        leaf = _terminal(("a", "x"), parent=None, evidence_ids=("ev-a",))
        parent = _internal((leaf,), parent=None)
        leaf = leaf.__class__(
            **{**leaf.__dict__, "parent_cluster_id": parent.cluster_id, "native_depth": 1}
        )
        return leaf, parent

    def test_valid_lineage_has_no_native_depth_findings(self) -> None:
        leaf, parent = self._two_level_tree()
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[leaf, parent],
            memberships=_memberships((leaf,)),
        )
        report = validate_derived_hierarchy(outcome)
        depth_codes = {
            f.code
            for f in report.findings
            if f.code
            in {
                "invalid_native_depth",
                "root_native_depth_not_zero",
                "native_depth_not_parent_plus_one",
            }
        }
        assert depth_codes == set()

    def test_negative_native_depth_caught(self) -> None:
        leaf, parent = self._two_level_tree()
        broken = leaf.__class__(**{**leaf.__dict__, "native_depth": -1})
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[broken, parent],
            memberships=_memberships((broken,)),
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "invalid_native_depth" for f in report.findings)

    def test_bool_native_depth_caught(self) -> None:
        # bool is a subtype of int in Python — a stray True/False must not
        # be silently accepted as a valid depth.
        leaf, parent = self._two_level_tree()
        broken = leaf.__class__(**{**leaf.__dict__, "native_depth": True})
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[broken, parent],
            memberships=_memberships((broken,)),
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "invalid_native_depth" for f in report.findings)

    def test_non_int_native_depth_caught(self) -> None:
        leaf, parent = self._two_level_tree()
        broken = leaf.__class__(**{**leaf.__dict__, "native_depth": "1"})
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[broken, parent],
            memberships=_memberships((broken,)),
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "invalid_native_depth" for f in report.findings)

    def test_root_with_nonzero_native_depth_caught(self) -> None:
        leaf, parent = self._two_level_tree()
        broken_root = parent.__class__(**{**parent.__dict__, "native_depth": 2})
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[leaf, broken_root],
            memberships=_memberships((leaf,)),
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "root_native_depth_not_zero" for f in report.findings)

    def test_child_depth_not_parent_plus_one_caught(self) -> None:
        leaf, parent = self._two_level_tree()
        # Parent is depth 0 (root); a correct child would be depth 1, but
        # this one skips straight to depth 3.
        broken_child = leaf.__class__(**{**leaf.__dict__, "native_depth": 3})
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[broken_child, parent],
            memberships=_memberships((broken_child,)),
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "native_depth_not_parent_plus_one" for f in report.findings)

    def test_singleton_isolate_native_depth_none_is_unaffected(self) -> None:
        # A singleton isolate's native_depth is legitimately None (never
        # assigned by the native partitioner) and must not trip the new
        # ancestor-lineage checks.
        leaf = _terminal(("a",), parent=None)
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE, clusters=[leaf], memberships=_memberships((leaf,))
        )
        report = validate_derived_hierarchy(outcome)
        depth_codes = {
            f.code
            for f in report.findings
            if f.code
            in {
                "invalid_native_depth",
                "root_native_depth_not_zero",
                "native_depth_not_parent_plus_one",
            }
        }
        assert depth_codes == set()

    @pytest.mark.parametrize(
        "malformed_parent_depth",
        ["bad", 1.5, True, -1],
        ids=["string", "float", "bool", "negative"],
    )
    def test_malformed_parent_native_depth_does_not_crash(
        self, malformed_parent_depth: object
    ) -> None:
        # Regression: a malformed (non-None) parent.native_depth must never
        # reach the `parent.native_depth + 1` arithmetic. Previously only
        # `parent.native_depth is None` was treated as "already reported
        # elsewhere, skip the lineage check" — any other malformed value
        # (string/float/bool/negative) fell through to the addition and
        # raised a raw TypeError (string) or produced a nonsensical
        # (bool/float/negative) comparison, crashing public validation /
        # build_safe instead of surfacing a ValidationFinding. The child
        # here is a genuinely valid int (1) so the crash can only be
        # triggered by the parent side of the comparison.
        leaf, parent = self._two_level_tree()
        broken_parent = parent.__class__(
            **{**parent.__dict__, "native_depth": malformed_parent_depth}
        )
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[leaf, broken_parent],
            memberships=_memberships((leaf,)),
        )
        # Must not raise — this is the core regression assertion.
        report = validate_derived_hierarchy(outcome)
        # The malformed parent row itself must still be flagged (either as
        # invalid_native_depth for a non-int/negative value, or — since
        # `True`/`-1` are also rejected by the same int/negativity check —
        # consistently as invalid_native_depth across every malformed case
        # here, because none of these four values is a valid non-negative
        # exact int).
        assert any(f.code == "invalid_native_depth" for f in report.findings)
        # And no spurious native_depth_not_parent_plus_one finding should
        # be emitted for the child against a parent depth that is itself
        # already known-invalid (that would be redundant noise, not a new
        # distinct lineage defect).
        assert not any(f.code == "native_depth_not_parent_plus_one" for f in report.findings)


class TestMemberConservationValidator:
    def test_duplicate_member_in_cluster_caught(self) -> None:
        leaf = _terminal(("a", "a"), parent=None)
        outcome = DerivedHierarchyOutcome(status=HierarchyStatus.COMPLETE, clusters=[leaf], memberships=[])
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "duplicate_member_in_cluster" for f in report.findings)

    def test_member_set_not_child_union_caught(self) -> None:
        leaf = _terminal(("a",), parent=None)
        parent = _internal((leaf,), parent=None)
        leaf = leaf.__class__(**{**leaf.__dict__, "parent_cluster_id": parent.cluster_id})
        broken_parent = parent.__class__(
            **{**parent.__dict__, "member_entity_ids": ("a", "extra-entity-not-a-child-member")}
        )
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[leaf, broken_parent],
            memberships=_memberships((leaf,)),
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "member_set_not_child_union" for f in report.findings)

    def test_terminal_member_set_mismatch_with_memberships_caught(self) -> None:
        leaf = _terminal(("a", "b"), parent=None)
        # Memberships only record "a", omitting "b" (dropped entity).
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[leaf],
            memberships=[DerivedMembershipRow(cluster_id=leaf.cluster_id, entity_id="a", evidence_ids=())],
        )
        report = validate_derived_hierarchy(outcome)
        assert any(
            f.code == "terminal_member_set_mismatch_with_memberships" for f in report.findings
        )


class TestOneTerminalMembershipValidator:
    def test_entity_with_two_terminal_memberships_caught(self) -> None:
        memberships = [
            DerivedMembershipRow(cluster_id="cluster-1", entity_id="a", evidence_ids=()),
            DerivedMembershipRow(cluster_id="cluster-2", entity_id="a", evidence_ids=()),
        ]
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE, clusters=[], memberships=memberships
        )
        report = validate_derived_hierarchy(outcome)
        assert any(
            f.code == "entity_has_multiple_terminal_memberships" for f in report.findings
        )


class TestEvidencePropagationValidator:
    def test_evidence_not_child_union_caught(self) -> None:
        leaf = _terminal(("a",), parent=None, evidence_ids=("ev-a",))
        parent = _internal((leaf,), parent=None)
        leaf = leaf.__class__(**{**leaf.__dict__, "parent_cluster_id": parent.cluster_id})
        broken_parent = parent.__class__(**{**parent.__dict__, "evidence_ids": ("ev-a", "ev-phantom")})
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[leaf, broken_parent],
            memberships=_memberships((leaf,)),
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "evidence_not_child_union" for f in report.findings)

    def test_relationship_evidence_not_subset_of_cluster_evidence_caught(self) -> None:
        leaf = _terminal(
            ("a",),
            parent=None,
            evidence_ids=("ev-a",),
            relationship_contributed_evidence_ids=("ev-not-in-evidence-ids",),
        )
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE, clusters=[leaf], memberships=_memberships((leaf,))
        )
        report = validate_derived_hierarchy(outcome)
        assert any(
            f.code == "relationship_evidence_not_subset_of_cluster_evidence"
            for f in report.findings
        )

    def test_internal_cluster_with_relationship_contributed_evidence_caught(self) -> None:
        leaf = _terminal(("a",), parent=None, evidence_ids=("ev-a",))
        parent = _internal((leaf,), parent=None)
        leaf = leaf.__class__(**{**leaf.__dict__, "parent_cluster_id": parent.cluster_id})
        broken_parent = parent.__class__(
            **{
                **parent.__dict__,
                "relationship_contributed_evidence_ids": ("ev-a",),
                "evidence_ids": ("ev-a",),
            }
        )
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[leaf, broken_parent],
            memberships=_memberships((leaf,)),
        )
        report = validate_derived_hierarchy(outcome)
        assert any(
            f.code == "internal_cluster_has_relationship_contributed_evidence"
            for f in report.findings
        )


class TestIdentityPermutationRepeatabilityValidator:
    def test_tampered_cluster_id_caught(self) -> None:
        leaf = _terminal(("a",), parent=None)
        tampered = leaf.__class__(**{**leaf.__dict__, "cluster_id": "not-the-real-computed-identity"})
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.COMPLETE,
            clusters=[tampered],
            memberships=[
                DerivedMembershipRow(cluster_id=tampered.cluster_id, entity_id="a", evidence_ids=())
            ],
        )
        report = validate_derived_hierarchy(outcome)
        assert any(f.code == "identity_not_permutation_repeatable" for f in report.findings)


class TestAssertValidDerivedHierarchy:
    def test_raises_with_all_findings_listed(self) -> None:
        leaf = _terminal(("a", "a"), parent=None)
        outcome = DerivedHierarchyOutcome(status=HierarchyStatus.COMPLETE, clusters=[leaf], memberships=[])
        with pytest.raises(AssertionError) as excinfo:
            assert_valid_derived_hierarchy(outcome)
        assert "duplicate_member_in_cluster" in str(excinfo.value)
