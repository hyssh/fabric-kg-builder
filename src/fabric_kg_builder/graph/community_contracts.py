"""Shared contracts for the versioned derived community hierarchy (v2).

This module is additive and independent of ``graph/community.py`` (the legacy
fixed-3-level ``hierarchy_version = "1.0"`` builder, which remains unchanged
and is the default when no provider is selected). It defines the data shapes
shared by ``community_projection.py``, ``community_provider.py``,
``community_identity.py``, ``community_hierarchy_v2.py`` and
``community_validation.py``.

Design notes (the evidence baseline these contracts respond to):

* Community *identity* never derives from display labels or native
  (``graspologic_native``) integer cluster ids — both are unstable across
  runs/permutations and collide across unrelated clusters with the same
  label (the legacy ``_cluster_id`` bug). Identity instead derives from
  (domain, provider/policy identity, canonical sorted member id set,
  structural lineage of child identities) — see ``community_identity.py``.
* ``level`` in the legacy schema conflated "distance from root" with
  "distance from leaf" across a fixed 3-level tree. The v2 contract keeps
  these separate and explicit: ``native_depth`` (root-first, as reported by
  the native provider) and ``structural_height`` (leaf-first; terminal
  clusters are height 0), with real parent links for navigation.
* ``max_cluster_size`` is a split trigger, not a hard cap: a terminal
  cluster that could not be subdivided further by the provider is retained
  and flagged ``is_oversized`` rather than silently truncated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

# Distinguishes v2 derived-hierarchy records from legacy ``hierarchy_version
# = "1.0"`` rows. Never used to overwrite/compare against the legacy value.
DERIVED_HIERARCHY_VERSION = "2.0-derived-leiden"

# Exact, pinned native dependency per assignment scope. No other version and
# no fallback algorithm is in scope; the provider that wraps this package is
# lazily imported (see community_provider.py) so core modules never hard
# import it.
NATIVE_PACKAGE_NAME = "graspologic-native"
NATIVE_PACKAGE_VERSION = "1.2.5"

# graspologic-native==1.2.5's own published wheels declare
# `Requires-Python: >=3.8,<3.14`. This documents that external constraint
# (confirmed empirically: resolving/installing it under Python 3.14 fails),
# not this project's own `requires-python` (">=3.10", see pyproject.toml).
# community_provider.py checks the running interpreter against this exact
# range before attempting the import, so an out-of-range interpreter (e.g.
# this project's own Python 3.14 default) fails with a distinct, explicit
# ``NativeProviderUnsupportedInterpreterError`` rather than an opaque
# ``ImportError`` or a silent assumption that the pin is available.
NATIVE_PACKAGE_MIN_PYTHON = (3, 8)
NATIVE_PACKAGE_MAX_PYTHON_EXCLUSIVE = (3, 14)

# Stable, importable diagnostic/reason values. These are the exact strings
# written to ``*Diagnostic.reason`` fields and ``DerivedHierarchyOutcome
# .reason`` for the corresponding conditions; consumers (e.g. downstream
# adapters/tests in other worktrees) should import these constants rather
# than hardcode or guess the literal values, so a future rename is a single
# edit here instead of a silent cross-repo contract break.
REASON_SELF_LOOP_EXCLUDED = "self_loop_excluded_from_partition_graph"
REASON_DUPLICATE_ENTITY_ID = "duplicate_entity_id_in_canonical_projection_input"
REASON_INTER_COMMUNITY_RELATIONSHIP_EVIDENCE_DUAL_ATTRIBUTION = (
    "relationship_spans_two_terminal_clusters_evidence_attributed_to_both"
)
#: Raised (never silently continued) when two or more relationship rows
#: share one ``relationship_id``. Fail-closed: even content-identical
#: duplicates are rejected, since a non-unique id breaks every downstream
#: ``relationship_by_id`` lookup (evidence attribution, inter-community
#: diagnostics) that assumes it is a primary key.
REASON_DUPLICATE_RELATIONSHIP_ID = "duplicate_relationship_id_in_canonical_projection_input"
#: Raised (never silently continued/dropped) when a relationship's source
#: and/or target entity id does not appear in the input entity set. An
#: unknown endpoint is a referential-integrity violation of the input
#: snapshot, not a condition to skip past.
REASON_UNKNOWN_RELATIONSHIP_ENDPOINT = "unknown_relationship_endpoint_in_canonical_projection_input"
#: ``DerivedHierarchyOutcome.reason`` for ``HierarchyStatus.EMPTY``.
REASON_EMPTY_ENTITY_INPUT = "empty_entity_input"
#: ``DerivedHierarchyOutcome.reason`` for ``HierarchyStatus.INSUFFICIENT``
#: (nonempty entities, zero projection edges/all self-loops). The full
#: human-readable sentence, not a short code, since this reason is surfaced
#: directly to operators/pipeline logs explaining why no real hierarchy was
#: built.
REASON_INSUFFICIENT_NO_CONNECTING_RELATIONSHIPS = (
    "No non-self-loop relationships connect any entities in this "
    "snapshot; every entity is retained as its own singleton "
    "terminal community. This is explicitly insufficient for "
    "meaningful semantic abstraction — no synthetic topic layer "
    "is invented."
)


class HierarchyStatus(str, Enum):
    """Outcome status of a derived-hierarchy build attempt."""

    #: Nonempty, edged input; a real (possibly single-node) hierarchy was built.
    COMPLETE = "complete"
    #: Empty entity input. Complete-but-empty projection; not an error.
    EMPTY = "empty"
    #: Nonempty input with zero projection edges (or all self-loops): every
    #: entity is its own terminal community; explicitly insufficient for
    #: meaningful abstraction. No padded/fake topic layer is invented.
    INSUFFICIENT = "insufficient"
    #: The configured provider's native dependency is not importable/available.
    #: No algorithmic fallback is substituted; the caller must install the
    #: pinned dependency to proceed.
    NATIVE_UNAVAILABLE = "native_unavailable"
    #: The provider ran but returned data this module could not validate
    #: against its structural assumptions (see community_validation.py).
    FAILED = "failed"


@dataclass(frozen=True)
class SelfLoopDiagnostic:
    """A relationship excluded from the undirected partition graph because
    its source and target resolve to the same canonical entity. Recorded,
    never silently dropped."""

    entity_id: str
    relationship_id: str
    relationship_type: str
    reason: str = REASON_SELF_LOOP_EXCLUDED


@dataclass(frozen=True)
class DuplicateEntityDiagnostic:
    """Recorded when the input entity set contains a non-unique
    ``entity_id`` — a canonical-projection precondition violation that is
    reported rather than silently deduplicated."""

    entity_id: str
    occurrence_count: int
    reason: str = REASON_DUPLICATE_ENTITY_ID


@dataclass(frozen=True)
class DuplicateRelationshipIdDiagnostic:
    """Recorded when the input relationship set contains a non-unique
    ``relationship_id``. ``conflicting`` is ``True`` when the duplicate
    rows disagree on source/target/type (the strictly worse case); it is
    ``False`` for byte-identical duplicates. Either way the duplicate is a
    fail-closed error, never a last-write-wins silent resolution."""

    relationship_id: str
    occurrence_count: int
    conflicting: bool
    reason: str = REASON_DUPLICATE_RELATIONSHIP_ID


@dataclass(frozen=True)
class UnknownRelationshipEndpointDiagnostic:
    """Recorded when a relationship's source and/or target entity id is not
    present in the input entity set. Both endpoints are reported even if
    only one is unknown (the known one as context); never silently
    skipped."""

    relationship_id: str
    relationship_type: str
    source_entity_id: str
    target_entity_id: str
    unknown_source: bool
    unknown_target: bool
    reason: str = REASON_UNKNOWN_RELATIONSHIP_ENDPOINT


@dataclass(frozen=True)
class ProjectionEdge:
    """One canonical, undirected, weight-bearing projection edge.

    Endpoints are stored as a sorted pair (``source_entity_id <
    target_entity_id`` lexicographically) so that reciprocal/duplicate
    directed relationship rows collapse onto exactly one projection edge.
    ``weight`` is always ``1.0`` for a distinct pair: duplicates/reciprocals
    contribute additional provenance, never additional weight.
    """

    source_entity_id: str
    target_entity_id: str
    weight: float
    contributing_relationship_ids: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalProjection:
    """Canonical undirected projection graph derived from the directed,
    typed ``RelationshipRow`` set.

    The original directed rows are never mutated or replaced; this is a
    sidecar, partition-ready view with explicit provenance back to them.
    """

    node_ids: tuple[str, ...]
    edges: tuple[ProjectionEdge, ...]
    self_loop_diagnostics: tuple[SelfLoopDiagnostic, ...]
    duplicate_entity_diagnostics: tuple[DuplicateEntityDiagnostic, ...]
    isolate_ids: tuple[str, ...]
    input_graph_hash: str


@dataclass(frozen=True)
class InterCommunityRelationshipDiagnostic:
    """Recorded whenever one relationship's two endpoints resolve to two
    *different* terminal clusters (possible even within one connected
    component: Leiden partitioning does not guarantee adjacent nodes land
    in the same leaf community). The relationship's evidence is still
    attributed to **both** endpoint clusters/ancestors — never dropped,
    never arbitrarily assigned to only one side — and this record is the
    explicit, auditable documentation of that dual attribution."""

    relationship_id: str
    relationship_type: str
    source_entity_id: str
    target_entity_id: str
    source_cluster_id: str
    target_cluster_id: str
    reason: str = REASON_INTER_COMMUNITY_RELATIONSHIP_EVIDENCE_DUAL_ATTRIBUTION


@dataclass(frozen=True)
class NativePartitionEntry:
    """One row of the native provider's flat hierarchical partition output.

    Field names/semantics mirror ``graspologic_native.hierarchical_leiden``'s
    ``HierarchicalCluster`` as used by GraphRAG's pinned
    ``graphs/hierarchical_leiden.py`` /``cluster_graph.py`` (commit
    769542fbf1d8e5b4c6a8677fefc34621c87894c5): ``level`` is root-first
    (0 at the coarsest/first level); ``is_final_cluster`` marks the node's
    own terminal (finest-grained) assignment, which may occur at different
    levels for different nodes in an unbalanced tree.
    """

    node_id: str
    cluster_id: int
    parent_cluster_id: Optional[int]
    level: int
    is_final_cluster: bool


@runtime_checkable
class CommunityProvider(Protocol):
    """Injectable partition provider boundary.

    Exactly one real implementation is in scope for this assignment: a lazy
    wrapper around ``graspologic_native==1.2.5``'s ``hierarchical_leiden``.
    Tests inject a fake/stub implementing this protocol so the core
    hierarchy/validation logic can be fully exercised without the native
    package installed.
    """

    provider_id: str
    provider_version: str

    def partition(
        self,
        edges: list[tuple[str, str, float]],
        *,
        max_cluster_size: int,
        seed: int,
    ) -> list[NativePartitionEntry]:
        """Partition an undirected weighted edge list into a hierarchical
        set of communities. ``edges`` must already be canonically sorted by
        the caller for permutation-repeatable results."""
        ...


class NativeProviderUnavailableError(RuntimeError):
    """Raised when the native provider's dependency is not importable.

    This is an explicit failure, never a silent algorithmic fallback.
    """

    def __init__(self, package_name: str = NATIVE_PACKAGE_NAME, package_version: str = NATIVE_PACKAGE_VERSION):
        self.package_name = package_name
        self.package_version = package_version
        super().__init__(
            f"Native community-partition dependency '{package_name}=={package_version}' "
            "is not installed. No algorithmic fallback is available for the v2 derived "
            "hierarchy provider; install exactly this pinned version to proceed."
        )


class NativeProviderUnsupportedInterpreterError(NativeProviderUnavailableError):
    """Raised when the running Python interpreter falls outside
    ``graspologic-native==1.2.5``'s own published ``Requires-Python``
    range (``>=3.8,<3.14``).

    Distinct from "not installed": no import is even attempted, because an
    out-of-range interpreter cannot have a compatible wheel resolved for it
    in the first place. A subclass of ``NativeProviderUnavailableError`` so
    every existing caller that already treats "native unavailable" as an
    explicit, non-fallback failure (never silently falling back to an
    algorithm, never assuming the extra being selected implies
    availability) automatically treats an unsupported interpreter the same
    way.
    """

    def __init__(self, running_version: tuple[int, int, int]):
        self.running_version = running_version
        running_str = ".".join(str(part) for part in running_version[:2])
        min_str = ".".join(str(part) for part in NATIVE_PACKAGE_MIN_PYTHON)
        max_str = ".".join(str(part) for part in NATIVE_PACKAGE_MAX_PYTHON_EXCLUSIVE)
        # Deliberately skip NativeProviderUnavailableError.__init__ (its
        # "not installed" message would be misleading here) and build our
        # own explicit interpreter-mismatch message via RuntimeError
        # directly, matching NativeProviderVersionMismatchError's pattern.
        RuntimeError.__init__(
            self,
            f"Running Python {running_str} is outside the interpreter range "
            f"'{NATIVE_PACKAGE_NAME}=={NATIVE_PACKAGE_VERSION}' publishes "
            f"wheels for ([{min_str}, {max_str})). Selecting the optional "
            "'leiden' extra on this interpreter does NOT make native Leiden "
            "available: no import was attempted, and no algorithmic "
            "fallback exists. Use a Python 3.10-3.13 interpreter (the "
            "intersection of this project's own requires-python>=3.10 and "
            "the native package's upstream <3.14 ceiling) to install and "
            "use this provider."
        )
        self.package_name = NATIVE_PACKAGE_NAME
        self.package_version = NATIVE_PACKAGE_VERSION


@dataclass(frozen=True)
class DerivedClusterRow:
    """One community node in the v2 derived hierarchy tree.

    Distinct from the legacy ``ClusterRow`` schema (reserved for
    ``hierarchy_version = "1.0"``): this record separates native root-first
    depth from leaf-first structural height, stores full identity lineage,
    and flags oversized/singleton/insufficient terminals explicitly.
    """

    cluster_id: str
    hierarchy_version: str
    parent_cluster_id: Optional[str]
    structural_height: int
    native_depth: Optional[int]
    is_terminal: bool
    is_singleton_isolate: bool
    is_oversized: bool
    member_entity_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    provider_id: str
    provider_version: str
    policy_id: str
    domain_hash: Optional[str]
    run_id: str
    execution_fingerprint: str
    #: For a terminal cluster only: the subset of ``evidence_ids`` that came
    #: from incident-relationship evidence (self-loop or projection-edge
    #: attribution) rather than from member/entity evidence. ``evidence_ids``
    #: is always the full union of both sources; this field documents just
    #: the relationship-sourced portion so validators and downstream
    #: consumers do not need to re-derive attribution themselves. Always
    #: empty for internal (non-terminal) clusters — relationship evidence is
    #: attributed only at the terminal cluster that directly owns the
    #: entity, then propagated upward as part of ``evidence_ids`` like any
    #: other evidence.
    relationship_contributed_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DerivedMembershipRow:
    """Exactly one terminal-community membership per original entity."""

    cluster_id: str
    entity_id: str
    evidence_ids: tuple[str, ...]
    primary_membership: bool = True


@dataclass
class DerivedHierarchyOutcome:
    """Result of ``build_derived_community_hierarchy``."""

    status: HierarchyStatus
    reason: Optional[str] = None
    clusters: list[DerivedClusterRow] = field(default_factory=list)
    memberships: list[DerivedMembershipRow] = field(default_factory=list)
    projection: Optional[CanonicalProjection] = None
    oversized_terminal_cluster_ids: tuple[str, ...] = ()
    #: Relationships whose two endpoints resolved to two different terminal
    #: clusters. Evidence from each such relationship is attributed to BOTH
    #: endpoint clusters/ancestors (never dropped, never picked arbitrarily);
    #: this field is the explicit, auditable record of every case where that
    #: dual-attribution happened. Always empty for ``EMPTY``/``INSUFFICIENT``
    #: outcomes (no edges to span in either case).
    inter_community_relationship_diagnostics: tuple[InterCommunityRelationshipDiagnostic, ...] = ()
    input_graph_hash: Optional[str] = None
    execution_fingerprint: Optional[str] = None
    provider_id: Optional[str] = None
    provider_version: Optional[str] = None
    hierarchy_version: str = DERIVED_HIERARCHY_VERSION
