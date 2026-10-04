"""Structural/semantic validators for a ``DerivedHierarchyOutcome`` (v2).

These validators are deliberately independent of any specific
``CommunityProvider`` implementation (native or test double): they only
inspect the already-built ``clusters``/``memberships``/``projection`` on a
``DerivedHierarchyOutcome`` and report every violation they find, rather
than raising on the first one, so a single call surfaces the complete set
of problems in one pass (useful for tests exercising deliberately-broken
provider output, and for CI diagnostics).

Scope (per assignment): tree/parent-link well-formedness, member-set
conservation (no entity gained/lost/duplicated), one terminal membership
per original entity, bottom-up evidence-union propagation (including the
relationship-contributed portion added by ``community_hierarchy_v2``'s
``_attach_relationship_evidence``), and identity repeatability under
canonical (sorted) member-set permutation. This module never re-runs the
provider and never mutates the outcome it inspects.
"""

from __future__ import annotations

from dataclasses import dataclass

from fabric_kg_builder.graph.community_contracts import (
    DerivedClusterRow,
    DerivedHierarchyOutcome,
    DerivedMembershipRow,
    HierarchyStatus,
)
from fabric_kg_builder.graph.community_identity import compute_community_identity


@dataclass(frozen=True)
class ValidationFinding:
    """One concrete validation failure. ``code`` is a stable, grep-able
    identifier; ``detail`` carries the specific offending ids/values."""

    code: str
    detail: str


@dataclass(frozen=True)
class ValidationReport:
    """Aggregate result of validating one ``DerivedHierarchyOutcome``.

    ``is_valid`` is ``True`` iff ``findings`` is empty. A report is still
    produced (not raised) even for non-``COMPLETE``/``INSUFFICIENT``
    outcomes so callers can assert "no findings" uniformly; most checks are
    simply vacuously satisfied (e.g. zero clusters) for ``EMPTY``.
    """

    findings: tuple[ValidationFinding, ...]

    @property
    def is_valid(self) -> bool:
        return not self.findings


def validate_derived_hierarchy(outcome: DerivedHierarchyOutcome) -> ValidationReport:
    """Run every structural/semantic validator and return one aggregate
    report. Does not raise; callers that want a hard failure should assert
    on ``report.is_valid`` (or inspect ``report.findings`` directly)."""
    findings: list[ValidationFinding] = []
    findings.extend(_validate_tree_structure(outcome.clusters))
    findings.extend(_validate_member_conservation(outcome.clusters, outcome.memberships))
    findings.extend(_validate_one_terminal_membership_per_entity(outcome.memberships))
    findings.extend(_validate_evidence_propagation(outcome.clusters))
    findings.extend(_validate_identity_permutation_repeatability(outcome.clusters))
    findings.extend(_validate_no_duplicate_cluster_ids(outcome.clusters))
    findings.extend(_validate_primary_membership_flag(outcome.memberships))
    findings.extend(_validate_no_empty_terminal_clusters(outcome.clusters))
    findings.extend(_validate_native_depth(outcome.clusters))
    findings.extend(_validate_projection_conservation(outcome))
    return ValidationReport(findings=tuple(findings))


def _validate_tree_structure(clusters: list[DerivedClusterRow]) -> list[ValidationFinding]:
    """Every non-root ``parent_cluster_id`` must reference a cluster that
    actually exists in this outcome; there must be no parent cycle; a
    cluster's ``structural_height`` must be strictly greater than every
    direct child's (and exactly ``1 + max(child heights)`` when it has
    children, ``0`` when it is terminal)."""
    findings: list[ValidationFinding] = []
    by_id = {c.cluster_id: c for c in clusters}

    for c in clusters:
        if c.parent_cluster_id is not None and c.parent_cluster_id not in by_id:
            findings.append(
                ValidationFinding(
                    "dangling_parent_reference",
                    f"cluster {c.cluster_id!r} has parent_cluster_id "
                    f"{c.parent_cluster_id!r} which does not exist in this outcome",
                )
            )

    children_by_parent: dict[str, list[str]] = {}
    for c in clusters:
        if c.parent_cluster_id is not None:
            children_by_parent.setdefault(c.parent_cluster_id, []).append(c.cluster_id)

    # Cycle detection: walk up parent links from every cluster; a cycle
    # means we never reach a root (None parent) within len(clusters) hops.
    for c in clusters:
        seen: set[str] = set()
        current: str | None = c.cluster_id
        hops = 0
        while current is not None:
            if current in seen:
                findings.append(
                    ValidationFinding(
                        "parent_cycle",
                        f"cluster {c.cluster_id!r} is part of a parent-link cycle "
                        f"(revisited {current!r})",
                    )
                )
                break
            seen.add(current)
            node = by_id.get(current)
            if node is None:
                break  # already reported as dangling_parent_reference above
            current = node.parent_cluster_id
            hops += 1
            if hops > len(clusters) + 1:
                findings.append(
                    ValidationFinding(
                        "parent_cycle",
                        f"cluster {c.cluster_id!r}'s parent chain exceeds the "
                        "total cluster count without reaching a root; treating "
                        "as a cycle",
                    )
                )
                break

    for c in clusters:
        child_ids = children_by_parent.get(c.cluster_id, ())
        if c.is_terminal:
            if child_ids:
                findings.append(
                    ValidationFinding(
                        "terminal_cluster_has_children",
                        f"cluster {c.cluster_id!r} is marked is_terminal=True but "
                        f"has child cluster(s) {sorted(child_ids)!r}",
                    )
                )
            if c.structural_height != 0:
                findings.append(
                    ValidationFinding(
                        "terminal_cluster_nonzero_height",
                        f"cluster {c.cluster_id!r} is terminal but "
                        f"structural_height={c.structural_height} (expected 0)",
                    )
                )
        else:
            if not child_ids:
                findings.append(
                    ValidationFinding(
                        "internal_cluster_has_no_children",
                        f"cluster {c.cluster_id!r} is marked is_terminal=False but "
                        "has no child clusters in this outcome",
                    )
                )
                continue
            expected_height = 1 + max(by_id[cid].structural_height for cid in child_ids)
            if c.structural_height != expected_height:
                findings.append(
                    ValidationFinding(
                        "incorrect_structural_height",
                        f"cluster {c.cluster_id!r} has structural_height="
                        f"{c.structural_height}, expected {expected_height} "
                        "(1 + max child height)",
                    )
                )

    return findings


def _validate_member_conservation(
    clusters: list[DerivedClusterRow], memberships: list[DerivedMembershipRow]
) -> list[ValidationFinding]:
    """A cluster's ``member_entity_ids`` must equal exactly the union of its
    children's member sets (for internal clusters) or exactly its own
    direct terminal members (for terminal clusters, cross-checked against
    ``memberships``) — never more, never fewer, never duplicated."""
    findings: list[ValidationFinding] = []
    by_id = {c.cluster_id: c for c in clusters}
    children_by_parent: dict[str, list[str]] = {}
    for c in clusters:
        if c.parent_cluster_id is not None:
            children_by_parent.setdefault(c.parent_cluster_id, []).append(c.cluster_id)

    for c in clusters:
        if len(c.member_entity_ids) != len(set(c.member_entity_ids)):
            findings.append(
                ValidationFinding(
                    "duplicate_member_in_cluster",
                    f"cluster {c.cluster_id!r} has duplicate entries in "
                    f"member_entity_ids: {c.member_entity_ids!r}",
                )
            )
        if not c.is_terminal:
            child_ids = children_by_parent.get(c.cluster_id, ())
            expected = set()
            for cid in child_ids:
                expected.update(by_id[cid].member_entity_ids)
            if set(c.member_entity_ids) != expected:
                findings.append(
                    ValidationFinding(
                        "member_set_not_child_union",
                        f"cluster {c.cluster_id!r} member_entity_ids does not "
                        "equal the union of its children's member sets "
                        f"(got {sorted(c.member_entity_ids)!r}, expected "
                        f"{sorted(expected)!r})",
                    )
                )

    terminal_members_by_cluster: dict[str, set[str]] = {}
    for m in memberships:
        terminal_members_by_cluster.setdefault(m.cluster_id, set()).add(m.entity_id)
    for c in clusters:
        if not c.is_terminal:
            continue
        expected = terminal_members_by_cluster.get(c.cluster_id, set())
        if set(c.member_entity_ids) != expected:
            findings.append(
                ValidationFinding(
                    "terminal_member_set_mismatch_with_memberships",
                    f"terminal cluster {c.cluster_id!r} member_entity_ids "
                    f"{sorted(c.member_entity_ids)!r} does not match the entity "
                    f"ids with a DerivedMembershipRow into it {sorted(expected)!r}",
                )
            )

    return findings


def _validate_one_terminal_membership_per_entity(
    memberships: list[DerivedMembershipRow],
) -> list[ValidationFinding]:
    """Every original entity must appear in exactly one terminal-cluster
    membership row — never zero (dropped), never more than one (entity
    assigned to two different terminal communities)."""
    findings: list[ValidationFinding] = []
    seen: dict[str, list[str]] = {}
    for m in memberships:
        seen.setdefault(m.entity_id, []).append(m.cluster_id)
    for entity_id, cluster_ids in seen.items():
        if len(cluster_ids) > 1:
            findings.append(
                ValidationFinding(
                    "entity_has_multiple_terminal_memberships",
                    f"entity {entity_id!r} has {len(cluster_ids)} terminal "
                    f"memberships: {sorted(cluster_ids)!r} (expected exactly 1)",
                )
            )
    return findings


def _validate_evidence_propagation(clusters: list[DerivedClusterRow]) -> list[ValidationFinding]:
    """Every internal (non-terminal) cluster's ``evidence_ids`` must equal
    exactly the union of its direct children's ``evidence_ids`` — this is
    the invariant ``_repropagate_evidence`` re-establishes after
    relationship-evidence attribution, and it must hold regardless of
    whether any relationship evidence was actually attached. Terminal
    clusters are not checked here against member evidence directly (that
    union also includes relationship-contributed evidence, which this
    module cannot recompute without the original projection/relationship
    inputs); terminal ``relationship_contributed_evidence_ids`` must always
    be a subset of ``evidence_ids`` and must always be empty on internal
    clusters."""
    findings: list[ValidationFinding] = []
    by_id = {c.cluster_id: c for c in clusters}
    children_by_parent: dict[str, list[str]] = {}
    for c in clusters:
        if c.parent_cluster_id is not None:
            children_by_parent.setdefault(c.parent_cluster_id, []).append(c.cluster_id)

    for c in clusters:
        if not set(c.relationship_contributed_evidence_ids) <= set(c.evidence_ids):
            findings.append(
                ValidationFinding(
                    "relationship_evidence_not_subset_of_cluster_evidence",
                    f"cluster {c.cluster_id!r} relationship_contributed_evidence_ids "
                    f"{c.relationship_contributed_evidence_ids!r} is not a subset of "
                    f"evidence_ids {c.evidence_ids!r}",
                )
            )
        if not c.is_terminal and c.relationship_contributed_evidence_ids:
            findings.append(
                ValidationFinding(
                    "internal_cluster_has_relationship_contributed_evidence",
                    f"internal cluster {c.cluster_id!r} has non-empty "
                    f"relationship_contributed_evidence_ids "
                    f"{c.relationship_contributed_evidence_ids!r}; relationship "
                    "evidence must only be attributed at terminal clusters",
                )
            )
        if not c.is_terminal:
            child_ids = children_by_parent.get(c.cluster_id, ())
            expected_evidence = {e for cid in child_ids for e in by_id[cid].evidence_ids}
            if set(c.evidence_ids) != expected_evidence:
                findings.append(
                    ValidationFinding(
                        "evidence_not_child_union",
                        f"internal cluster {c.cluster_id!r} evidence_ids does not "
                        "equal the union of its children's evidence_ids (got "
                        f"{sorted(c.evidence_ids)!r}, expected "
                        f"{sorted(expected_evidence)!r})",
                    )
                )

    return findings


def _validate_identity_permutation_repeatability(
    clusters: list[DerivedClusterRow],
) -> list[ValidationFinding]:
    """Recomputing each cluster's identity from its own recorded
    (domain, provider, policy, member set, structural lineage) must
    reproduce ``cluster_id`` exactly, independent of member-set iteration
    order (``compute_community_identity`` sorts defensively) — i.e. the
    identity a caller observes is actually a pure function of those
    canonical inputs, not an artifact of this particular build's internal
    bookkeeping/native-id numbering."""
    findings: list[ValidationFinding] = []
    by_id = {c.cluster_id: c for c in clusters}
    children_by_parent: dict[str, list[str]] = {}
    for c in clusters:
        if c.parent_cluster_id is not None:
            children_by_parent.setdefault(c.parent_cluster_id, []).append(c.cluster_id)

    for c in clusters:
        child_ids = children_by_parent.get(c.cluster_id, ())
        lineage = tuple(sorted(by_id[cid].cluster_id for cid in child_ids))
        recomputed = compute_community_identity(
            domain_hash=c.domain_hash,
            provider_id=c.provider_id,
            provider_version=c.provider_version,
            policy_id=c.policy_id,
            member_entity_ids=tuple(reversed(c.member_entity_ids)),  # deliberately permuted
            structural_lineage=lineage,
        )
        if recomputed != c.cluster_id:
            findings.append(
                ValidationFinding(
                    "identity_not_permutation_repeatable",
                    f"cluster {c.cluster_id!r} does not match its own "
                    f"recomputed identity {recomputed!r} from "
                    "(domain_hash, provider_id, provider_version, policy_id, "
                    "member_entity_ids, structural_lineage); identity may be "
                    "sensitive to member-set order or lineage is incomplete",
                )
            )

    return findings


def _validate_no_duplicate_cluster_ids(clusters: list[DerivedClusterRow]) -> list[ValidationFinding]:
    """Every ``cluster_id`` in an outcome must be unique — two distinct
    rows sharing an identity would make parent/child/member-conservation
    checks ambiguous and would mean two structurally different clusters
    collapsed onto the same identity string."""
    seen: dict[str, int] = {}
    for c in clusters:
        seen[c.cluster_id] = seen.get(c.cluster_id, 0) + 1
    return [
        ValidationFinding(
            "duplicate_cluster_id",
            f"cluster_id {cid!r} appears {count} times in outcome.clusters",
        )
        for cid, count in seen.items()
        if count > 1
    ]


def _validate_primary_membership_flag(
    memberships: list[DerivedMembershipRow],
) -> list[ValidationFinding]:
    """v2's contract is exactly-one-terminal-membership-per-entity with no
    secondary/non-primary memberships (unlike the legacy multi-assignment
    model); every membership row this builder produces must therefore have
    ``primary_membership=True``."""
    return [
        ValidationFinding(
            "non_primary_membership_in_v2",
            f"membership for entity {m.entity_id!r} into cluster "
            f"{m.cluster_id!r} has primary_membership=False; v2's "
            "exactly-one-terminal-membership contract means every "
            "membership is primary",
        )
        for m in memberships
        if not m.primary_membership
    ]


def _validate_no_empty_terminal_clusters(
    clusters: list[DerivedClusterRow],
) -> list[ValidationFinding]:
    """A terminal cluster with an empty member set is not a meaningful
    community — it should never have been emitted (isolates get their own
    singleton terminal; a terminal with zero members is a builder bug, not
    a valid degenerate case)."""
    return [
        ValidationFinding(
            "empty_terminal_cluster",
            f"terminal cluster {c.cluster_id!r} has an empty member_entity_ids",
        )
        for c in clusters
        if c.is_terminal and not c.member_entity_ids
    ]


def _validate_native_depth(clusters: list[DerivedClusterRow]) -> list[ValidationFinding]:
    """``native_depth`` must be ``None`` iff the cluster is a singleton
    isolate (singletons bypass the provider entirely and never get a
    native depth); a concrete, non-negative int otherwise. For non-isolate
    clusters the native, root-first depth must also form a coherent
    per-node ancestor lineage: a root cluster (no parent) must report
    depth 0, and every non-root cluster's depth must be exactly its
    parent's depth + 1 — anything else means the reported ``level``
    values across the provider's flat entry rows do not actually describe
    a single consistent root-first tree for this node's ancestor chain."""
    findings: list[ValidationFinding] = []
    by_id = {c.cluster_id: c for c in clusters}
    for c in clusters:
        if c.is_singleton_isolate:
            if c.native_depth is not None:
                findings.append(
                    ValidationFinding(
                        "singleton_isolate_has_native_depth",
                        f"cluster {c.cluster_id!r} is a singleton isolate but "
                        f"native_depth={c.native_depth!r} (expected None)",
                    )
                )
            continue

        if c.native_depth is None:
            findings.append(
                ValidationFinding(
                    "non_isolate_missing_native_depth",
                    f"cluster {c.cluster_id!r} is not a singleton isolate but "
                    "native_depth is None",
                )
            )
            continue

        if isinstance(c.native_depth, bool) or not isinstance(c.native_depth, int) or c.native_depth < 0:
            findings.append(
                ValidationFinding(
                    "invalid_native_depth",
                    f"cluster {c.cluster_id!r} has native_depth={c.native_depth!r}; "
                    "expected a non-negative int",
                )
            )
            continue

        if c.parent_cluster_id is None:
            if c.native_depth != 0:
                findings.append(
                    ValidationFinding(
                        "root_native_depth_not_zero",
                        f"root cluster {c.cluster_id!r} has native_depth="
                        f"{c.native_depth!r}; a root-first depth must start at 0",
                    )
                )
            continue

        parent = by_id.get(c.parent_cluster_id)
        if parent is None:
            # Already reported by _validate_tree_structure's dangling-parent
            # check; avoid a redundant/confusing second finding here.
            continue
        if (
            parent.native_depth is None
            or isinstance(parent.native_depth, bool)
            or not isinstance(parent.native_depth, int)
            or parent.native_depth < 0
        ):
            # The parent row itself will be visited by this same loop (every
            # cluster is iterated over as `c`) and will be flagged there —
            # as non_isolate_missing_native_depth if None, or as
            # invalid_native_depth for any other non-int/negative value.
            # Without this guard, a malformed non-None parent.native_depth
            # (e.g. a string/float/bool/negative) would reach the
            # `parent.native_depth + 1` arithmetic below and raise a raw
            # TypeError/crash instead of surfacing as a ValidationFinding.
            continue
        if c.native_depth != parent.native_depth + 1:
            findings.append(
                ValidationFinding(
                    "native_depth_not_parent_plus_one",
                    f"cluster {c.cluster_id!r} has native_depth={c.native_depth!r} "
                    f"but parent {c.parent_cluster_id!r} has native_depth="
                    f"{parent.native_depth!r}; expected parent_depth + 1 "
                    f"({parent.native_depth + 1!r})",
                )
            )
    return findings


def _validate_projection_conservation(
    outcome: DerivedHierarchyOutcome,
) -> list[ValidationFinding]:
    """Cross-check the terminal memberships against the outcome's own
    canonical projection (when present): every projection node id must
    have exactly one terminal membership, no membership may reference an
    entity id outside the projection, and every membership's cluster_id
    must target a cluster that actually exists in this outcome and is
    terminal. A no-op when ``outcome.projection`` is ``None`` (e.g. an
    ``EMPTY``/``NATIVE_UNAVAILABLE``/``FAILED`` outcome with no projection
    to compare against)."""
    if outcome.projection is None:
        return []
    findings: list[ValidationFinding] = []
    by_id = {c.cluster_id: c for c in outcome.clusters}
    expected_node_ids = set(outcome.projection.node_ids)
    membership_entity_ids = {m.entity_id for m in outcome.memberships}

    missing = expected_node_ids - membership_entity_ids
    if missing:
        findings.append(
            ValidationFinding(
                "entity_missing_terminal_membership",
                f"entity id(s) {sorted(missing)!r} present in the canonical "
                "projection but have no terminal-cluster membership row",
            )
        )
    extra = membership_entity_ids - expected_node_ids
    if extra:
        findings.append(
            ValidationFinding(
                "membership_entity_not_in_projection",
                f"membership row(s) reference entity id(s) {sorted(extra)!r} "
                "that are not present in the canonical projection's node_ids",
            )
        )
    for m in outcome.memberships:
        target = by_id.get(m.cluster_id)
        if target is None:
            findings.append(
                ValidationFinding(
                    "membership_targets_nonexistent_cluster",
                    f"membership for entity {m.entity_id!r} targets "
                    f"cluster_id {m.cluster_id!r} which does not exist in "
                    "this outcome's clusters",
                )
            )
        elif not target.is_terminal:
            findings.append(
                ValidationFinding(
                    "membership_targets_nonterminal_cluster",
                    f"membership for entity {m.entity_id!r} targets "
                    f"cluster_id {m.cluster_id!r} which is not a terminal "
                    "cluster",
                )
            )
    return findings


def assert_valid_derived_hierarchy(outcome: DerivedHierarchyOutcome) -> None:
    """Convenience assertion wrapper: raises ``AssertionError`` with every
    finding listed if ``outcome`` fails any validator. For
    ``NATIVE_UNAVAILABLE``/``FAILED`` outcomes, callers should check
    ``outcome.status`` themselves first — this function still runs
    (vacuously, since ``clusters``/``memberships`` are typically empty for
    those statuses) but is not meant to assert on *why* a build failed."""
    report = validate_derived_hierarchy(outcome)
    if not report.is_valid:
        details = "\n".join(f"- [{f.code}] {f.detail}" for f in report.findings)
        raise AssertionError(
            f"Derived hierarchy validation failed with {len(report.findings)} "
            f"finding(s) (status={outcome.status}):\n{details}"
        )
