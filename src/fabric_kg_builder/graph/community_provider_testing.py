"""TEST-ONLY community-partition provider fake. DO NOT WIRE INTO PRODUCTION.

This module exists solely so unit tests can exercise the v2 derived
hierarchy / validation logic (tree construction, identity, conservation,
permutation-repeatability) end-to-end without the real
``graspologic-native==1.2.5`` dependency installed in this environment.

``DeterministicConnectedComponentsProvider`` is deliberately kept out of
``community_provider.py`` (the production provider module) and is NOT
importable from it, so it can never be mistaken for, or accidentally
wired in as, an operator-usable substitute for real Leiden partitioning.
Any code path that constructs a provider for an actual pipeline run must
import ``NativeLeidenProvider`` from ``community_provider`` — never this
module. Orchestration/CLI/config code must not reference this module at
all outside of its own test suite.

This fake does NOT implement Leiden/modularity optimization. It only
satisfies the ``CommunityProvider`` protocol deterministically (plain
undirected connected components, split into canonical-order chunks only
when a component exceeds ``max_cluster_size``, per "split trigger, not
hard cap" semantics). Real native-partition quality/behavior is explicitly
NOT validated by tests that use this fake; those tests are documented and
marked as blocked pending ``graspologic-native==1.2.5`` availability.
"""

from __future__ import annotations

from fabric_kg_builder.graph.community_contracts import (
    CommunityProvider,
    NativePartitionEntry,
)


class DeterministicConnectedComponentsProvider:
    """A dependency-free ``CommunityProvider`` fake for tests only.

    Partitioning strategy (deterministic, order-independent given already
    canonically-sorted input edges): group nodes into plain undirected
    connected components (level 0 = terminal, one cluster per component),
    then — only if a component exceeds ``max_cluster_size`` — split it
    further into contiguous canonical-order chunks under one synthetic
    parent (level 1): a chunk that is still oversized by itself is
    retained as-is (split trigger, not hard cap).
    """

    provider_id = "stub_connected_components"
    provider_version = "test-fake-1"

    def partition(
        self,
        edges: list[tuple[str, str, float]],
        *,
        max_cluster_size: int,
        seed: int,
    ) -> list[NativePartitionEntry]:
        del seed  # deterministic regardless of seed; documented limitation
        adjacency: dict[str, set[str]] = {}
        for source, target, _weight in edges:
            adjacency.setdefault(source, set()).add(target)
            adjacency.setdefault(target, set()).add(source)

        visited: set[str] = set()
        components: list[list[str]] = []
        for node in sorted(adjacency):
            if node in visited:
                continue
            stack = [node]
            visited.add(node)
            component: list[str] = []
            while stack:
                current = stack.pop()
                component.append(current)
                for neighbor in sorted(adjacency.get(current, ())):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        stack.append(neighbor)
            components.append(sorted(component))

        entries: list[NativePartitionEntry] = []
        next_cluster_id = 0
        for component in components:
            if len(component) <= max_cluster_size:
                cid = next_cluster_id
                next_cluster_id += 1
                for node in component:
                    entries.append(
                        NativePartitionEntry(
                            node_id=node,
                            cluster_id=cid,
                            parent_cluster_id=None,
                            level=0,
                            is_final_cluster=True,
                        )
                    )
                continue

            parent_cid = next_cluster_id
            next_cluster_id += 1
            # Root-level (non-final) rows for every node in this component,
            # establishing parent_cid's own cluster_id -> parent_cluster_id
            # (None) mapping. Mirrors the real native provider's output
            # shape: a node has one row per level it passes through, with
            # is_final_cluster True only at its own terminal level — a
            # purely internal (pass-through) cluster id would otherwise
            # never appear in cluster_parent at all and could never become
            # a tree root.
            for node in component:
                entries.append(
                    NativePartitionEntry(
                        node_id=node,
                        cluster_id=parent_cid,
                        parent_cluster_id=None,
                        level=0,
                        is_final_cluster=False,
                    )
                )
            chunk_size = max(1, max_cluster_size)
            for i in range(0, len(component), chunk_size):
                child_cid = next_cluster_id
                next_cluster_id += 1
                chunk = component[i : i + chunk_size]
                for node in chunk:
                    entries.append(
                        NativePartitionEntry(
                            node_id=node,
                            cluster_id=child_cid,
                            parent_cluster_id=parent_cid,
                            level=1,
                            is_final_cluster=True,
                        )
                    )
        return entries


class FixedPartitionProvider:
    """A ``CommunityProvider`` fake that returns a caller-supplied, fixed
    list of ``NativePartitionEntry`` rows regardless of the ``edges``
    passed to ``partition`` — i.e. it ignores the real input graph.

    This exists ONLY to let tests construct deliberately broken/adversarial
    provider output (missing nodes, unknown node ids, dangling parent
    references, disconnected cycles, duplicate cluster ids, etc.) and
    assert that ``build_derived_community_hierarchy`` detects and rejects
    it via the conservation validators in ``community_validation.py``,
    rather than silently returning a corrupted ``COMPLETE`` outcome. It
    must never be used to model realistic partition behavior — use
    ``DeterministicConnectedComponentsProvider`` for that.
    """

    provider_id = "stub_fixed_partition"
    provider_version = "test-fake-1"

    def __init__(self, entries: list[NativePartitionEntry]) -> None:
        self._entries = entries

    def partition(
        self,
        edges: list[tuple[str, str, float]],
        *,
        max_cluster_size: int,
        seed: int,
    ) -> list[NativePartitionEntry]:
        del edges, max_cluster_size, seed
        return list(self._entries)


# Runtime sanity check: fail import-time if this fake ever drifts out of
# protocol conformance with CommunityProvider (caught here, not at some
# later call site in test code).
assert isinstance(DeterministicConnectedComponentsProvider(), CommunityProvider)
assert isinstance(FixedPartitionProvider([]), CommunityProvider)


__all__ = [
    "DeterministicConnectedComponentsProvider",
    "FixedPartitionProvider",
]
