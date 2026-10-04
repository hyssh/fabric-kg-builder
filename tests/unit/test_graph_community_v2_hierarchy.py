"""Tests for graph/community_hierarchy_v2.py — v2 derived community
hierarchy build (additive to, and independent of, the legacy
graph/community.py::build_community_hierarchy builder, hierarchy_version
= "1.0", which is untouched by this module and remains the default).

Uses the injectable ``DeterministicConnectedComponentsProvider`` test fake
(see community_provider_testing.py) since the real
``graspologic-native==1.2.5`` dependency is not installed in this
environment (package installation is not authorized for this offline
assignment). Tests using this fake validate tree/conservation/evidence
structural invariants only — they do NOT validate real Leiden partition
quality, which remains an open, explicitly-reported native-dependency gap.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from fabric_kg_builder.graph.community_contracts import (
    REASON_EMPTY_ENTITY_INPUT,
    REASON_INSUFFICIENT_NO_CONNECTING_RELATIONSHIPS,
    HierarchyStatus,
    NativePartitionEntry,
    NativeProviderUnavailableError,
)
from fabric_kg_builder.graph.community_hierarchy_v2 import (
    DerivedHierarchyBuildError,
    DerivedHierarchyConfigError,
    build_derived_community_hierarchy,
    build_derived_community_hierarchy_safe,
)
from fabric_kg_builder.graph.community_projection import CanonicalProjectionError
from fabric_kg_builder.graph.community_provider import NativeLeidenProvider, is_native_available
from fabric_kg_builder.graph.community_provider_testing import (
    DeterministicConnectedComponentsProvider,
    FixedPartitionProvider,
)
from fabric_kg_builder.model.schemas import EntityRow, RelationshipRow

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(
    entity_id: str, run_id: str = "run-1", domain_hash: str | None = None
) -> EntityRow:
    key = hashlib.sha1(entity_id.encode()).hexdigest()[:16]
    return EntityRow(
        entity_id=entity_id,
        entity_type="org",
        display_name=entity_id,
        canonical_key=key,
        content_hash=key,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        run_id=run_id,
        domain_hash=domain_hash,
    )


def _entities(ids: list[str], run_id: str = "run-1") -> list[EntityRow]:
    return [_entity(i, run_id=run_id) for i in ids]


def _rel(rel_id: str, source: str, target: str, rel_type: str = "knows") -> RelationshipRow:
    key = hashlib.sha1(rel_id.encode()).hexdigest()[:16]
    return RelationshipRow(
        relationship_id=rel_id,
        relationship_type=rel_type,
        source_entity_id=source,
        target_entity_id=target,
        content_hash=key,
        created_at=datetime.now(timezone.utc),
    )


def _chain_fixture(ids: list[str]) -> tuple[list[EntityRow], list[RelationshipRow]]:
    """A single connected chain a-b-c-...-z over the given ids."""
    entities = _entities(ids)
    relationships = [
        _rel(f"r{i}", ids[i], ids[i + 1]) for i in range(len(ids) - 1)
    ]
    return entities, relationships


PROVIDER = DeterministicConnectedComponentsProvider()


# ---------------------------------------------------------------------------
# Completeness branches
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_empty_entities_yields_empty_status(self) -> None:
        outcome = build_derived_community_hierarchy(
            [], [], provider=PROVIDER, policy_id="policy-a", run_id="run-1"
        )
        assert outcome.status == HierarchyStatus.EMPTY
        assert outcome.reason == REASON_EMPTY_ENTITY_INPUT
        assert outcome.clusters == []
        assert outcome.memberships == []


class TestInsufficientInput:
    def test_nonempty_edgeless_entities_yield_insufficient_singleton_terminals(self) -> None:
        entities = _entities(["a", "b", "c"])
        outcome = build_derived_community_hierarchy(
            entities, [], provider=PROVIDER, policy_id="policy-a", run_id="run-1"
        )
        assert outcome.status == HierarchyStatus.INSUFFICIENT
        assert outcome.reason == REASON_INSUFFICIENT_NO_CONNECTING_RELATIONSHIPS
        # Conserved: every entity retained as its own singleton terminal,
        # never padded/fabricated topics.
        assert len(outcome.clusters) == 3
        assert all(c.is_terminal and c.is_singleton_isolate for c in outcome.clusters)
        assert {m.entity_id for m in outcome.memberships} == {"a", "b", "c"}

    def test_self_loop_only_relationships_are_also_insufficient(self) -> None:
        entities = _entities(["a", "b"])
        relationships = [_rel("r1", "a", "a"), _rel("r2", "b", "b")]
        outcome = build_derived_community_hierarchy(
            entities, relationships, provider=PROVIDER, policy_id="policy-a", run_id="run-1"
        )
        assert outcome.status == HierarchyStatus.INSUFFICIENT
        assert len(outcome.clusters) == 2


class TestCompleteInput:
    def test_connected_pair_yields_complete(self) -> None:
        entities, relationships = _chain_fixture(["a", "b"])
        outcome = build_derived_community_hierarchy(
            entities, relationships, provider=PROVIDER, policy_id="policy-a", run_id="run-1"
        )
        assert outcome.status == HierarchyStatus.COMPLETE
        assert outcome.reason is None

    def test_mixed_connected_and_isolate_entities_all_conserved(self) -> None:
        entities = _entities(["a", "b", "iso"])
        relationships = [_rel("r1", "a", "b")]
        outcome = build_derived_community_hierarchy(
            entities, relationships, provider=PROVIDER, policy_id="policy-a", run_id="run-1"
        )
        assert outcome.status == HierarchyStatus.COMPLETE
        all_members: set[str] = set()
        for cluster in outcome.clusters:
            all_members |= set(cluster.member_entity_ids)
        assert {m.entity_id for m in outcome.memberships} == {"a", "b", "iso"}
        # Exactly one terminal membership per original entity.
        terminal_cluster_ids = {c.cluster_id for c in outcome.clusters if c.is_terminal}
        memberships_by_entity: dict[str, int] = {}
        for m in outcome.memberships:
            if m.cluster_id in terminal_cluster_ids:
                memberships_by_entity[m.entity_id] = memberships_by_entity.get(m.entity_id, 0) + 1
        assert all(count == 1 for count in memberships_by_entity.values())

    def test_oversized_component_reported_not_truncated(self) -> None:
        ids = [f"n{i}" for i in range(7)]
        entities, relationships = _chain_fixture(ids)
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=PROVIDER,
            policy_id="policy-a",
            run_id="run-1",
            max_cluster_size=3,
        )
        assert outcome.status == HierarchyStatus.COMPLETE
        # max_cluster_size is a split trigger: chunks may still be
        # oversized by themselves and must be retained/reported, not
        # silently truncated.
        all_members: set[str] = set()
        for cluster in outcome.clusters:
            if cluster.is_terminal:
                all_members |= set(cluster.member_entity_ids)
        assert all_members == set(ids)

    def test_structural_height_is_leaf_first_not_native_depth_arithmetic(self) -> None:
        # A component large enough to be split (internal parent + terminal
        # children) so native_depth (root-first) and structural_height
        # (leaf-first, from actual parent links) are observably distinct.
        ids = [f"n{i}" for i in range(5)]
        entities, relationships = _chain_fixture(ids)
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=PROVIDER,
            policy_id="policy-a",
            run_id="run-1",
            max_cluster_size=2,
        )
        terminals = [c for c in outcome.clusters if c.is_terminal]
        internals = [c for c in outcome.clusters if not c.is_terminal]
        assert internals, "expected at least one internal (split-parent) cluster"
        assert all(c.structural_height == 0 for c in terminals)
        for internal in internals:
            children_heights = [
                c.structural_height for c in outcome.clusters if c.parent_cluster_id == internal.cluster_id
            ]
            assert internal.structural_height == max(children_heights, default=-1) + 1


# ---------------------------------------------------------------------------
# Identity never derived from display labels / native integer ids
# ---------------------------------------------------------------------------


class TestClusterIdentity:
    def test_cluster_ids_are_not_raw_native_integers(self) -> None:
        entities, relationships = _chain_fixture(["a", "b", "c"])
        outcome = build_derived_community_hierarchy(
            entities, relationships, provider=PROVIDER, policy_id="policy-a", run_id="run-1"
        )
        for cluster in outcome.clusters:
            # Native partition entries use small integer cluster ids (0, 1,
            # 2, ...). Derived cluster_id must be a computed identity
            # string, never that raw integer stringified.
            assert not cluster.cluster_id.isdigit()

    def test_same_graph_permutations_yield_identical_execution_fingerprint(self) -> None:
        ids = ["a", "b", "c", "d"]
        entities, relationships = _chain_fixture(ids)
        outcome_1 = build_derived_community_hierarchy(
            entities, relationships, provider=PROVIDER, policy_id="policy-a", run_id="run-1"
        )
        outcome_2 = build_derived_community_hierarchy(
            list(reversed(entities)),
            list(reversed(relationships)),
            provider=PROVIDER,
            policy_id="policy-a",
            run_id="run-1",
        )
        assert outcome_1.execution_fingerprint == outcome_2.execution_fingerprint
        assert {c.cluster_id for c in outcome_1.clusters} == {
            c.cluster_id for c in outcome_2.clusters
        }

    def test_different_seed_changes_fingerprint(self) -> None:
        entities, relationships = _chain_fixture(["a", "b", "c"])
        outcome_1 = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=PROVIDER,
            policy_id="policy-a",
            run_id="run-1",
            seed=1,
        )
        outcome_2 = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=PROVIDER,
            policy_id="policy-a",
            run_id="run-1",
            seed=2,
        )
        assert outcome_1.execution_fingerprint != outcome_2.execution_fingerprint


# ---------------------------------------------------------------------------
# Relationship evidence sidecar
# ---------------------------------------------------------------------------


class TestRelationshipEvidence:
    def test_self_loop_evidence_attributed_to_owning_entity_terminal_cluster(self) -> None:
        entities = _entities(["a", "b"])
        relationships = [_rel("r1", "a", "b"), _rel("loop", "a", "a")]
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=PROVIDER,
            policy_id="policy-a",
            run_id="run-1",
            relationship_evidence={"loop": ["ev-loop"]},
        )
        a_terminal = next(
            c
            for c in outcome.clusters
            if c.is_terminal and "a" in c.member_entity_ids
        )
        assert "ev-loop" in a_terminal.evidence_ids
        assert "ev-loop" in a_terminal.relationship_contributed_evidence_ids

    def test_inter_community_relationship_evidence_dual_attributed_and_diagnosed(self) -> None:
        # Two disjoint pairs plus one bridging relationship spanning them
        # forces two different terminal clusters unless the fake provider
        # merges by connectivity: use a large max_cluster_size so each
        # 2-node pair is its own terminal, then add a cross relationship
        # NOT represented in the projection graph via relationship_evidence
        # (the scenario: evidence can span clusters even when the
        # relationship itself is the connecting edge).
        entities = _entities(["a", "b", "c"])
        relationships = [_rel("r1", "a", "b"), _rel("r2", "b", "c")]
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=DeterministicConnectedComponentsProvider(),
            policy_id="policy-a",
            run_id="run-1",
            max_cluster_size=1,
            relationship_evidence={"r1": ["ev1"], "r2": ["ev2"]},
        )
        # With max_cluster_size=1 every node becomes its own terminal under
        # a shared internal parent; r1/r2 span two different terminals
        # each, producing inter-community diagnostics.
        assert len(outcome.inter_community_relationship_diagnostics) == 2
        for diag in outcome.inter_community_relationship_diagnostics:
            assert diag.reason == (
                "relationship_spans_two_terminal_clusters_evidence_attributed_to_both"
            )

    def test_ancestor_evidence_is_union_of_children(self) -> None:
        ids = [f"n{i}" for i in range(4)]
        entities, relationships = _chain_fixture(ids)
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=PROVIDER,
            policy_id="policy-a",
            run_id="run-1",
            max_cluster_size=2,
            entity_evidence={i: [f"ev-{i}"] for i in ids},
        )
        internals = [c for c in outcome.clusters if not c.is_terminal]
        assert internals
        by_id = {c.cluster_id: c for c in outcome.clusters}
        for internal in internals:
            children = [c for c in outcome.clusters if c.parent_cluster_id == internal.cluster_id]
            expected = set()
            for child in children:
                expected |= set(child.evidence_ids)
            assert set(internal.evidence_ids) == expected


# ---------------------------------------------------------------------------
# Non-raising "safe" entrypoint and native-provider lazy loading
# ---------------------------------------------------------------------------


class TestSafeEntrypoint:
    def test_duplicate_entity_id_is_failed_not_raised(self) -> None:
        entities = _entities(["a", "a"])
        outcome = build_derived_community_hierarchy_safe(
            entities, [], provider=PROVIDER, policy_id="policy-a", run_id="run-1"
        )
        assert outcome.status == HierarchyStatus.FAILED
        assert outcome.reason is not None

    def test_duplicate_entity_id_raises_in_non_safe_variant(self) -> None:
        entities = _entities(["a", "a"])
        with pytest.raises(CanonicalProjectionError):
            build_derived_community_hierarchy(
                entities, [], provider=PROVIDER, policy_id="policy-a", run_id="run-1"
            )

    def test_native_unavailable_is_status_coded_not_raised(self) -> None:
        if is_native_available():
            pytest.skip(
                "graspologic-native is installed in this environment; "
                "the NATIVE_UNAVAILABLE branch cannot be exercised here."
            )
        entities, relationships = _chain_fixture(["a", "b"])
        outcome = build_derived_community_hierarchy_safe(
            entities,
            relationships,
            provider=NativeLeidenProvider(),
            policy_id="policy-a",
            run_id="run-1",
        )
        assert outcome.status == HierarchyStatus.NATIVE_UNAVAILABLE
        assert outcome.reason is not None

    def test_native_unavailable_raises_in_non_safe_variant(self) -> None:
        if is_native_available():
            pytest.skip(
                "graspologic-native is installed in this environment; "
                "the NativeProviderUnavailableError path cannot be exercised here."
            )
        entities, relationships = _chain_fixture(["a", "b"])
        with pytest.raises(NativeProviderUnavailableError):
            build_derived_community_hierarchy(
                entities,
                relationships,
                provider=NativeLeidenProvider(),
                policy_id="policy-a",
                run_id="run-1",
            )


# ---------------------------------------------------------------------------
# Defect #1 regression: fail-closed input validation (unknown endpoints,
# empty-branch must still validate nonempty relationships first).
# ---------------------------------------------------------------------------


class TestFailClosedInputValidation:
    def test_unknown_relationship_endpoint_raises_not_silently_skipped(self) -> None:
        entities = _entities(["a", "b"])
        relationships = [_rel("r1", "a", "ghost")]
        with pytest.raises(CanonicalProjectionError):
            build_derived_community_hierarchy(
                entities, relationships, provider=PROVIDER, policy_id="policy-a", run_id="run-1"
            )

    def test_empty_entities_with_nonempty_relationships_raises_not_empty_status(self) -> None:
        # Defect #1: the EMPTY branch must validate nonempty relationships
        # rather than returning a quiet EMPTY outcome before projection
        # checks run. With zero entities, every relationship endpoint is by
        # definition unknown, so this must raise CanonicalProjectionError —
        # never silently report HierarchyStatus.EMPTY.
        relationships = [_rel("r1", "a", "b")]
        with pytest.raises(CanonicalProjectionError):
            build_derived_community_hierarchy(
                [], relationships, provider=PROVIDER, policy_id="policy-a", run_id="run-1"
            )

    def test_duplicate_conflicting_relationship_id_raises_not_last_write_wins(self) -> None:
        entities = _entities(["a", "b", "c"])
        relationships = [
            _rel("r1", "a", "b"),
            _rel("r1", "a", "c"),  # same relationship_id, different endpoints
        ]
        with pytest.raises(CanonicalProjectionError):
            build_derived_community_hierarchy(
                entities, relationships, provider=PROVIDER, policy_id="policy-a", run_id="run-1"
            )


# ---------------------------------------------------------------------------
# Defect #3 regression (mandatory): InterCommunityRelationshipDiagnostic must
# use the relationship's own directed endpoints, not the sorted undirected
# edge pair — backward/reciprocal relationships must not have their
# direction corrupted or their source_cluster/target_cluster swapped.
# ---------------------------------------------------------------------------


class TestBackwardReciprocalDiagnosticDirection:
    def test_backward_relationship_direction_is_preserved_in_diagnostic(self) -> None:
        # The canonical edge for (a, b) is sorted as (a, b); construct the
        # relationship "backward" (target -> source) so edge.source !=
        # rel.source, which is exactly the case the old
        # edge.source/target-based diagnostic construction corrupted.
        entities = _entities(["a", "b", "c"])
        relationships = [_rel("r1", "b", "a"), _rel("r2", "b", "c")]
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=DeterministicConnectedComponentsProvider(),
            policy_id="policy-a",
            run_id="run-1",
            max_cluster_size=1,
            relationship_evidence={"r1": ["ev1"], "r2": ["ev2"]},
        )
        diagnostics_by_rel = {
            d.relationship_id: d for d in outcome.inter_community_relationship_diagnostics
        }
        assert set(diagnostics_by_rel) == {"r1", "r2"}
        diag_r1 = diagnostics_by_rel["r1"]
        # Must reflect rel.source_entity_id/target_entity_id ("b" -> "a"),
        # never the canonical sorted pair ("a", "b").
        assert diag_r1.source_entity_id == "b"
        assert diag_r1.target_entity_id == "a"
        cluster_by_entity = {
            m.entity_id: m.cluster_id
            for m in outcome.memberships
            if any(c.cluster_id == m.cluster_id and c.is_terminal for c in outcome.clusters)
        }
        assert diag_r1.source_cluster_id == cluster_by_entity["b"]
        assert diag_r1.target_cluster_id == cluster_by_entity["a"]

    def test_reciprocal_relationships_each_report_their_own_direction(self) -> None:
        # Two reciprocal relationships between the same pair: a->b and
        # b->a. Both must independently report their own correct directed
        # endpoints in the diagnostic, not a single scrambled/shared one.
        entities = _entities(["a", "b", "c"])
        relationships = [
            _rel("r_fwd", "a", "b"),
            _rel("r_back", "b", "a"),
            _rel("r_bridge", "b", "c"),
        ]
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=DeterministicConnectedComponentsProvider(),
            policy_id="policy-a",
            run_id="run-1",
            max_cluster_size=1,
            relationship_evidence={
                "r_fwd": ["ev_fwd"],
                "r_back": ["ev_back"],
                "r_bridge": ["ev_bridge"],
            },
        )
        diagnostics_by_rel = {
            d.relationship_id: d for d in outcome.inter_community_relationship_diagnostics
        }
        # r_fwd and r_back share the same canonical (a, b) edge but must
        # each retain their own directed endpoints in the diagnostic.
        assert diagnostics_by_rel["r_fwd"].source_entity_id == "a"
        assert diagnostics_by_rel["r_fwd"].target_entity_id == "b"
        assert diagnostics_by_rel["r_back"].source_entity_id == "b"
        assert diagnostics_by_rel["r_back"].target_entity_id == "a"
        # And their cluster ids must be swapped relative to each other,
        # consistent with their opposite directions (never both equal to
        # the same (source_cluster, target_cluster) pair).
        fwd = diagnostics_by_rel["r_fwd"]
        back = diagnostics_by_rel["r_back"]
        assert fwd.source_cluster_id == back.target_cluster_id
        assert fwd.target_cluster_id == back.source_cluster_id


# ---------------------------------------------------------------------------
# Defect #4 regression: coherent-domain / config validation.
# ---------------------------------------------------------------------------


class TestDomainAndConfigValidation:
    def test_mixed_domain_hash_input_raises(self) -> None:
        entities = [
            _entity("a", domain_hash="domain-1"),
            _entity("b", domain_hash="domain-2"),
        ]
        relationships = [_rel("r1", "a", "b")]
        with pytest.raises(DerivedHierarchyConfigError):
            build_derived_community_hierarchy(
                entities, relationships, provider=PROVIDER, policy_id="policy-a", run_id="run-1"
            )

    def test_uniform_domain_hash_input_succeeds(self) -> None:
        entities = [
            _entity("a", domain_hash="domain-1"),
            _entity("b", domain_hash="domain-1"),
        ]
        relationships = [_rel("r1", "a", "b")]
        outcome = build_derived_community_hierarchy(
            entities, relationships, provider=PROVIDER, policy_id="policy-a", run_id="run-1"
        )
        assert outcome.status == HierarchyStatus.COMPLETE

    @pytest.mark.parametrize("bad_max_cluster_size", [0, -1, 1.5, "3", True])
    def test_invalid_max_cluster_size_raises(self, bad_max_cluster_size: object) -> None:
        entities, relationships = _chain_fixture(["a", "b"])
        with pytest.raises(DerivedHierarchyConfigError):
            build_derived_community_hierarchy(
                entities,
                relationships,
                provider=PROVIDER,
                policy_id="policy-a",
                run_id="run-1",
                max_cluster_size=bad_max_cluster_size,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("bad_seed", [-1, 2**64, 1.5, "1", True])
    def test_invalid_seed_raises(self, bad_seed: object) -> None:
        entities, relationships = _chain_fixture(["a", "b"])
        with pytest.raises(DerivedHierarchyConfigError):
            build_derived_community_hierarchy(
                entities,
                relationships,
                provider=PROVIDER,
                policy_id="policy-a",
                run_id="run-1",
                seed=bad_seed,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Evidence sidecar validation (clarified requirement relayed from W4's
# independent reproduction): unknown keys and malformed value types must be
# rejected explicitly; absent keys for otherwise-valid ids remain allowed.
# ---------------------------------------------------------------------------


class TestEvidenceSidecarValidation:
    def test_unknown_entity_evidence_key_raises(self) -> None:
        entities, relationships = _chain_fixture(["a", "b"])
        with pytest.raises(DerivedHierarchyConfigError):
            build_derived_community_hierarchy(
                entities,
                relationships,
                provider=PROVIDER,
                policy_id="policy-a",
                run_id="run-1",
                entity_evidence={"ghost": ["ev1"]},
            )

    def test_bare_string_entity_evidence_value_raises(self) -> None:
        entities, relationships = _chain_fixture(["a", "b"])
        with pytest.raises(DerivedHierarchyConfigError):
            build_derived_community_hierarchy(
                entities,
                relationships,
                provider=PROVIDER,
                policy_id="policy-a",
                run_id="run-1",
                entity_evidence={"a": "ev1"},  # type: ignore[dict-item]
            )

    def test_unknown_relationship_evidence_key_raises(self) -> None:
        entities, relationships = _chain_fixture(["a", "b"])
        with pytest.raises(DerivedHierarchyConfigError):
            build_derived_community_hierarchy(
                entities,
                relationships,
                provider=PROVIDER,
                policy_id="policy-a",
                run_id="run-1",
                relationship_evidence={"ghost-rel": ["ev1"]},
            )

    def test_bare_string_relationship_evidence_value_raises(self) -> None:
        entities, relationships = _chain_fixture(["a", "b"])
        with pytest.raises(DerivedHierarchyConfigError):
            build_derived_community_hierarchy(
                entities,
                relationships,
                provider=PROVIDER,
                policy_id="policy-a",
                run_id="run-1",
                relationship_evidence={"r0": "ev1"},  # type: ignore[dict-item]
            )

    def test_absent_evidence_keys_for_valid_ids_are_allowed(self) -> None:
        entities, relationships = _chain_fixture(["a", "b"])
        outcome = build_derived_community_hierarchy(
            entities,
            relationships,
            provider=PROVIDER,
            policy_id="policy-a",
            run_id="run-1",
            entity_evidence={"a": ["ev1"]},  # "b" intentionally absent
            relationship_evidence={},  # "r0" intentionally absent
        )
        assert outcome.status == HierarchyStatus.COMPLETE


# ---------------------------------------------------------------------------
# Defect #2 regression (end-to-end): provider-output conservation must be
# enforced before a COMPLETE outcome is ever returned. Exercised via
# FixedPartitionProvider, which lets tests substitute arbitrary/adversarial
# NativePartitionEntry rows for the real partitioning step.
# ---------------------------------------------------------------------------


class TestProviderOutputConservation:
    def test_missing_connected_entity_in_provider_output_raises(self) -> None:
        # "b" is connected (a-b edge) but the provider's output only
        # accounts for "a" as a final-cluster member.
        entities, relationships = _chain_fixture(["a", "b"])
        provider = FixedPartitionProvider(
            [NativePartitionEntry("a", 0, None, 0, True)]
        )
        with pytest.raises(DerivedHierarchyBuildError):
            build_derived_community_hierarchy(
                entities, relationships, provider=provider, policy_id="policy-a", run_id="run-1"
            )

    def test_unknown_native_node_id_in_final_cluster_raises(self) -> None:
        entities, relationships = _chain_fixture(["a", "b"])
        provider = FixedPartitionProvider(
            [
                NativePartitionEntry("a", 0, None, 0, True),
                NativePartitionEntry("b", 0, None, 0, True),
                NativePartitionEntry("ghost-node", 1, None, 0, True),
            ]
        )
        with pytest.raises(DerivedHierarchyBuildError):
            build_derived_community_hierarchy(
                entities, relationships, provider=provider, policy_id="policy-a", run_id="run-1"
            )

    def test_unknown_native_node_id_in_non_final_row_raises(self) -> None:
        # Regression: the unknown-node-id check must apply to every
        # partition entry, not just rows where is_final_cluster is True.
        # A non-final ancestor row naming a node that was never one of the
        # input entities must be rejected just as loudly as a final row
        # would be — it must not be silently discarded.
        entities, relationships = _chain_fixture(["a", "b"])
        provider = FixedPartitionProvider(
            [
                NativePartitionEntry("a", 0, None, 0, True),
                NativePartitionEntry("b", 0, None, 0, True),
                NativePartitionEntry("ghost-node", 1, None, 0, False),
            ]
        )
        with pytest.raises(DerivedHierarchyBuildError):
            build_derived_community_hierarchy(
                entities, relationships, provider=provider, policy_id="policy-a", run_id="run-1"
            )

    def test_dangling_parent_reference_raises(self) -> None:
        entities, relationships = _chain_fixture(["a", "b"])
        provider = FixedPartitionProvider(
            [
                # cluster 0 claims parent 99, but 99 never appears as its
                # own cluster_id in any entry.
                NativePartitionEntry("a", 0, 99, 1, True),
                NativePartitionEntry("b", 0, 99, 1, True),
            ]
        )
        with pytest.raises(DerivedHierarchyBuildError):
            build_derived_community_hierarchy(
                entities, relationships, provider=provider, policy_id="policy-a", run_id="run-1"
            )

    def test_unreachable_disjoint_cluster_raises(self) -> None:
        # Cluster 0 (root, contains a+b) is well-formed and reachable.
        # Cluster 5 only ever appears as its own parent (self-referential,
        # never reachable as a child of any root) — a disjoint/cyclic
        # sub-structure the DFS from real roots can never visit.
        entities, relationships = _chain_fixture(["a", "b"])
        provider = FixedPartitionProvider(
            [
                NativePartitionEntry("a", 0, None, 0, True),
                NativePartitionEntry("b", 0, None, 0, True),
                NativePartitionEntry("a", 5, 5, 0, False),
            ]
        )
        with pytest.raises(DerivedHierarchyBuildError):
            build_derived_community_hierarchy(
                entities, relationships, provider=provider, policy_id="policy-a", run_id="run-1"
            )

    def test_well_formed_fixed_partition_still_succeeds(self) -> None:
        # Sanity check that FixedPartitionProvider itself is not the
        # source of failure — a correctly-conserving fixed output must
        # still produce a valid COMPLETE outcome.
        entities, relationships = _chain_fixture(["a", "b"])
        provider = FixedPartitionProvider(
            [
                NativePartitionEntry("a", 0, None, 0, True),
                NativePartitionEntry("b", 0, None, 0, True),
            ]
        )
        outcome = build_derived_community_hierarchy(
            entities, relationships, provider=provider, policy_id="policy-a", run_id="run-1"
        )
        assert outcome.status == HierarchyStatus.COMPLETE
        assert {m.entity_id for m in outcome.memberships} == {"a", "b"}
