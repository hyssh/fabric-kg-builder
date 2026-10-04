"""Main entry point for the v2 derived community hierarchy.

``build_derived_community_hierarchy`` is additive to, and independent of,
the legacy ``graph/community.py::build_community_hierarchy`` builder
(``hierarchy_version = "1.0"``), which remains unchanged and is still the
default when no provider is configured. This module implements
``DERIVED_HIERARCHY_VERSION`` using an injected ``CommunityProvider`` (see
community_provider.py) so it never hard-depends on ``graspologic_native``.

Branches (see docstrings on ``HierarchyStatus``):

* Empty entity input -> ``EMPTY``.
* Nonempty entities, zero projection edges (all isolated/self-loop-only)
  -> ``INSUFFICIENT``: every entity is its own singleton terminal, with an
  explicit reason — never a padded/fabricated topic layer.
* Otherwise -> ``COMPLETE``: isolates become singleton terminals directly
  (bypassing the provider, which has no edges to partition them on);
  connected entities are partitioned by the injected provider and folded
  into a real tree with post-order identity computation.

Depth/height: the provider's ``level`` (root-first, native) is kept
*separate* from a leaf-first ``structural_height`` computed here by
post-order walk over actual parent links — never ``max_depth - level``
arithmetic, since the native tree can be unbalanced across branches.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Optional, Sequence
from collections.abc import Mapping as ABCMapping, Sequence as ABCSequence

from fabric_kg_builder.graph.community_contracts import (
    DERIVED_HIERARCHY_VERSION,
    REASON_EMPTY_ENTITY_INPUT,
    REASON_INSUFFICIENT_NO_CONNECTING_RELATIONSHIPS,
    CanonicalProjection,
    CommunityProvider,
    DerivedClusterRow,
    DerivedHierarchyOutcome,
    DerivedMembershipRow,
    HierarchyStatus,
    InterCommunityRelationshipDiagnostic,
    NativePartitionEntry,
    NativeProviderUnavailableError,
)
from fabric_kg_builder.graph.community_identity import (
    compute_community_identity,
    compute_evidence_binding_hash,
    compute_execution_fingerprint,
)
from fabric_kg_builder.graph.community_projection import (
    CanonicalProjectionError,
    build_canonical_projection,
    to_native_edge_list,
)
from fabric_kg_builder.graph.community_validation import validate_derived_hierarchy
from fabric_kg_builder.model.schemas import EntityRow, RelationshipRow


class DerivedHierarchyBuildError(RuntimeError):
    """Raised when the provider's output cannot be reconciled into a valid
    hierarchy tree (structural assumption violated — see inline checks)."""


class DerivedHierarchyConfigError(ValueError):
    """Raised for malformed build configuration: incoherent mixed-domain
    input, an invalid ``max_cluster_size``/``seed``, or a structurally
    invalid ``entity_evidence``/``relationship_evidence`` sidecar. Distinct
    from ``DerivedHierarchyBuildError``, which is reserved for
    provider-output structural failures discovered after the provider has
    already run."""


def _resolve_domain_hash(entities: list[EntityRow]) -> Optional[str]:
    """A single derived-hierarchy build requires one coherent domain
    context (or none at all, if every entity has ``domain_hash=None``).
    Mixed-domain input must be rejected rather than arbitrarily resolved
    to ``entities[0]``'s domain (which would make the result sensitive to
    caller-side entity ordering)."""
    distinct = {e.domain_hash for e in entities}
    if len(distinct) > 1:
        raise DerivedHierarchyConfigError(
            f"entities span {len(distinct)} distinct domain_hash values "
            f"{sorted(str(d) for d in distinct)!r}; a single derived-hierarchy "
            "build requires one coherent domain context (or none at all) — "
            "split mixed-domain input into separate builds rather than "
            "letting the domain component be chosen arbitrarily"
        )
    return next(iter(distinct)) if distinct else None


def _validate_build_config(*, max_cluster_size: int, seed: int) -> None:
    """``max_cluster_size`` is a split *trigger*, not a hard cap (oversized
    terminals are retained and reported via ``oversized_terminal_cluster_ids``),
    but it must still be a positive int. ``seed`` must be an int within the
    native-supported unsigned 64-bit range."""
    if not isinstance(max_cluster_size, int) or isinstance(max_cluster_size, bool):
        raise DerivedHierarchyConfigError(
            f"max_cluster_size must be an int, got {type(max_cluster_size).__name__}"
        )
    if max_cluster_size < 1:
        raise DerivedHierarchyConfigError(
            "max_cluster_size must be >= 1 (it is a split trigger, not a hard "
            f"cap — oversized terminals are still retained/reported), got "
            f"{max_cluster_size!r}"
        )
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise DerivedHierarchyConfigError(f"seed must be an int, got {type(seed).__name__}")
    if not (0 <= seed < 2**64):
        raise DerivedHierarchyConfigError(
            "seed must be within the native-supported unsigned 64-bit range "
            f"[0, 2**64), got {seed!r}"
        )


def _validate_evidence_sidecar(
    name: str, sidecar: Optional[Mapping[str, Sequence[str]]], valid_ids: set[str]
) -> None:
    """An evidence sidecar must be a mapping whose keys are a subset of
    this build's own entity/relationship ids and whose values are
    sequences of strings (never a bare string, which iterates character by
    character, and never any other scalar/mapping type). Absent keys for
    otherwise-valid ids are allowed (explicit empty evidence); unknown keys
    and malformed value types are rejected explicitly rather than silently
    ignored or coerced."""
    if sidecar is None:
        return
    if not isinstance(sidecar, ABCMapping):
        raise DerivedHierarchyConfigError(
            f"{name} must be a mapping, got {type(sidecar).__name__}"
        )
    unknown_keys = set(sidecar.keys()) - valid_ids
    if unknown_keys:
        raise DerivedHierarchyConfigError(
            f"{name} has {len(unknown_keys)} key(s) not present in this "
            f"build's own ids: {sorted(unknown_keys)!r}"
        )
    for key, value in sidecar.items():
        if isinstance(value, (str, bytes)) or not isinstance(value, ABCSequence):
            raise DerivedHierarchyConfigError(
                f"{name}[{key!r}] must be a sequence of strings, got "
                f"{type(value).__name__} ({value!r})"
            )
        if not all(isinstance(v, str) for v in value):
            raise DerivedHierarchyConfigError(
                f"{name}[{key!r}] must contain only strings, got {value!r}"
            )


def _assert_outcome_structurally_valid(outcome: DerivedHierarchyOutcome) -> None:
    """Run every structural/semantic validator in ``community_validation``
    against a freshly-built ``COMPLETE``/``INSUFFICIENT`` outcome before it
    is returned to the caller. This is the single enforcement point that
    turns "the tree we just assembled looks right" into "the tree we just
    assembled is proven right" -- a provider that silently drops/duplicates
    entities, mislabels native depth, or produces a non-conserving tree
    must never reach a caller as a quiet COMPLETE."""
    report = validate_derived_hierarchy(outcome)
    if not report.is_valid:
        details = "; ".join(f"[{f.code}] {f.detail}" for f in report.findings)
        raise DerivedHierarchyBuildError(
            f"Built outcome failed {len(report.findings)} structural/semantic "
            f"validation check(s) and cannot be returned as {outcome.status}: "
            f"{details}"
        )


def build_derived_community_hierarchy(
    entities: list[EntityRow],
    relationships: list[RelationshipRow],
    *,
    provider: CommunityProvider,
    policy_id: str,
    run_id: str,
    config_id: str = "default",
    max_cluster_size: int = 10,
    seed: int = 0xDEADBEEF,
    entity_evidence: Optional[Mapping[str, Sequence[str]]] = None,
    relationship_evidence: Optional[Mapping[str, Sequence[str]]] = None,
) -> DerivedHierarchyOutcome:
    """Build the v2 derived community hierarchy for one entity/relationship
    snapshot using the given injected provider.

    ``entity_evidence`` is an optional, caller-validated evidence sidecar:
    a mapping of ``entity_id -> evidence ids`` (e.g. exact L3 evidence from
    the caller's own pipeline stage). When provided, it is the sole source
    of per-entity evidence for this build — entity ids present in
    ``entities`` but absent from this mapping get an explicit empty
    evidence tuple (never silently reused from a stale/partial source).
    When omitted, falls back to each ``EntityRow.evidence_ids`` (which may
    itself be ``None``/unset upstream — callers that need guaranteed,
    validated evidence propagation should pass ``entity_evidence``
    explicitly rather than relying on this fallback).

    ``relationship_evidence`` is the analogous optional sidecar keyed by
    canonical ``relationship_id``, for evidence that lives on the
    *relationship* rather than either endpoint entity (e.g. an upstream
    ``semantic_asserted_relationships.evidence_span_ids``-style field that
    has no destination on ``RelationshipRow`` itself in the caller's
    pipeline). Same authoritative-sidecar semantics as ``entity_evidence``:
    when provided it is the sole source for relationship ids it covers;
    relationship ids absent from it get an explicit empty evidence tuple.
    When omitted, falls back to each ``RelationshipRow.evidence_ids``
    (``None``/unset treated as empty) — this fallback is a plain mirror of
    ``entity_evidence``'s and is unrelated to upstream fields the caller's
    schema does not carry.

    Relationship evidence is folded into the hierarchy as follows, without
    ever rewriting the typed/directed ``RelationshipRow`` objects (this is
    sidecar enrichment of the hierarchy *artifacts* only):

    * A self-loop relationship's evidence is attributed to the
      self-looping entity's own terminal cluster (self-loops are already
      excluded from the canonical partition graph itself, but their
      evidence still belongs to the owning entity's community).
    * A projection edge's contributing relationships' evidence is
      attributed to **both** endpoints' terminal clusters. When the two
      endpoints resolve to the *same* terminal cluster this is a no-op
      duplicate (set union); when they resolve to *different* terminal
      clusters, the evidence is attributed to both sides (never dropped,
      never picked arbitrarily) and an ``InterCommunityRelationshipDiagnostic``
      records the dual attribution explicitly.
    * Each terminal cluster's relationship-sourced evidence ids are both
      folded into its ``evidence_ids`` (union with member evidence) and
      recorded separately in ``relationship_contributed_evidence_ids``.
    * Internal (non-terminal) cluster evidence is then re-propagated
      bottom-up so every ancestor's ``evidence_ids`` is still the union of
      its children's — preserving the propagation invariant even though
      relationship evidence was folded in after the tree was first built.

    Resolved evidence-sidecar content (both mappings, post-fallback) is
    bound into ``execution_fingerprint`` via ``compute_evidence_binding_hash``:
    two builds over an identical graph/config/seed with different evidence
    content produce different fingerprints, since evidence content is part
    of the output artifact (even though it is excluded from cluster
    identity itself).

    Does not catch ``NativeProviderUnavailableError`` or structural
    reconciliation errors (``DerivedHierarchyBuildError``,
    ``CanonicalProjectionError``) — callers that need a status-coded,
    non-raising outcome should use ``build_derived_community_hierarchy_safe``
    below, which converts all three into explicit, non-success outcomes.
    """
    _validate_build_config(max_cluster_size=max_cluster_size, seed=seed)
    _validate_evidence_sidecar(
        "entity_evidence", entity_evidence, {e.entity_id for e in entities}
    )
    _validate_evidence_sidecar(
        "relationship_evidence", relationship_evidence, {r.relationship_id for r in relationships}
    )

    projection = build_canonical_projection(entities, relationships)

    if not entities:
        return DerivedHierarchyOutcome(
            status=HierarchyStatus.EMPTY,
            reason=REASON_EMPTY_ENTITY_INPUT,
            provider_id=provider.provider_id,
            provider_version=provider.provider_version,
        )

    domain_hash = _resolve_domain_hash(entities)
    if entity_evidence is not None:
        resolved_entity_evidence: dict[str, tuple[str, ...]] = {
            e.entity_id: tuple(sorted(entity_evidence.get(e.entity_id, ()))) for e in entities
        }
    else:
        resolved_entity_evidence = {
            e.entity_id: tuple(sorted(e.evidence_ids or [])) for e in entities
        }
    entity_evidence = resolved_entity_evidence

    if relationship_evidence is not None:
        resolved_relationship_evidence: dict[str, tuple[str, ...]] = {
            r.relationship_id: tuple(sorted(relationship_evidence.get(r.relationship_id, ())))
            for r in relationships
        }
    else:
        resolved_relationship_evidence = {
            r.relationship_id: tuple(sorted(r.evidence_ids or [])) for r in relationships
        }
    relationship_evidence = resolved_relationship_evidence

    evidence_binding_hash = compute_evidence_binding_hash(entity_evidence, relationship_evidence)


    if not projection.edges:
        clusters, memberships = _build_singleton_terminals(
            projection.node_ids,
            entity_evidence,
            provider=provider,
            policy_id=policy_id,
            domain_hash=domain_hash,
            run_id=run_id,
            projection=projection,
            config_id=config_id,
            seed=seed,
        )
        clusters, inter_community_diagnostics = _attach_relationship_evidence(
            clusters,
            memberships,
            projection=projection,
            relationships=relationships,
            relationship_evidence=relationship_evidence,
        )
        fingerprint = compute_execution_fingerprint(
            input_graph_hash=projection.input_graph_hash,
            config_id=config_id,
            provider_id=provider.provider_id,
            provider_version=provider.provider_version,
            seed=seed,
            policy_id=policy_id,
            max_cluster_size=max_cluster_size,
            domain_hash=domain_hash,
            evidence_binding_hash=evidence_binding_hash,
        )
        clusters = [replace(c, execution_fingerprint=fingerprint) for c in clusters]
        outcome = DerivedHierarchyOutcome(
            status=HierarchyStatus.INSUFFICIENT,
            reason=REASON_INSUFFICIENT_NO_CONNECTING_RELATIONSHIPS,
            clusters=clusters,
            memberships=memberships,
            projection=projection,
            inter_community_relationship_diagnostics=inter_community_diagnostics,
            input_graph_hash=projection.input_graph_hash,
            execution_fingerprint=fingerprint,
            provider_id=provider.provider_id,
            provider_version=provider.provider_version,
        )
        _assert_outcome_structurally_valid(outcome)
        return outcome

    isolate_clusters, isolate_memberships = _build_singleton_terminals(
        projection.isolate_ids,
        entity_evidence,
        provider=provider,
        policy_id=policy_id,
        domain_hash=domain_hash,
        run_id=run_id,
        projection=projection,
        config_id=config_id,
        seed=seed,
    )

    edge_list = to_native_edge_list(projection)
    entries = provider.partition(edge_list, max_cluster_size=max_cluster_size, seed=seed)

    expected_node_ids = frozenset(projection.node_ids) - frozenset(projection.isolate_ids)
    connected_clusters, connected_memberships, oversized_ids = _build_tree_from_entries(
        entries,
        entity_evidence,
        provider=provider,
        policy_id=policy_id,
        domain_hash=domain_hash,
        run_id=run_id,
        max_cluster_size=max_cluster_size,
        expected_node_ids=expected_node_ids,
    )

    fingerprint = compute_execution_fingerprint(
        input_graph_hash=projection.input_graph_hash,
        config_id=config_id,
        provider_id=provider.provider_id,
        provider_version=provider.provider_version,
        seed=seed,
        policy_id=policy_id,
        max_cluster_size=max_cluster_size,
        domain_hash=domain_hash,
        evidence_binding_hash=evidence_binding_hash,
    )
    pre_clusters = [*isolate_clusters, *connected_clusters]
    all_memberships = [*isolate_memberships, *connected_memberships]
    all_clusters, inter_community_diagnostics = _attach_relationship_evidence(
        pre_clusters,
        all_memberships,
        projection=projection,
        relationships=relationships,
        relationship_evidence=relationship_evidence,
    )
    all_clusters = [replace(c, execution_fingerprint=fingerprint) for c in all_clusters]

    outcome = DerivedHierarchyOutcome(
        status=HierarchyStatus.COMPLETE,
        clusters=all_clusters,
        memberships=all_memberships,
        projection=projection,
        oversized_terminal_cluster_ids=oversized_ids,
        inter_community_relationship_diagnostics=inter_community_diagnostics,
        input_graph_hash=projection.input_graph_hash,
        execution_fingerprint=fingerprint,
        provider_id=provider.provider_id,
        provider_version=provider.provider_version,
    )
    _assert_outcome_structurally_valid(outcome)
    return outcome


def build_derived_community_hierarchy_safe(
    entities: list[EntityRow],
    relationships: list[RelationshipRow],
    *,
    provider: CommunityProvider,
    policy_id: str,
    run_id: str,
    config_id: str = "default",
    max_cluster_size: int = 10,
    seed: int = 0xDEADBEEF,
    entity_evidence: Optional[Mapping[str, Sequence[str]]] = None,
    relationship_evidence: Optional[Mapping[str, Sequence[str]]] = None,
) -> DerivedHierarchyOutcome:
    """Status-coded variant: converts every known non-success condition into
    an explicit, non-raising ``DerivedHierarchyOutcome`` instead of letting
    an exception propagate, for callers (e.g. orchestration/CLI layers)
    that want a uniform outcome object:

    * ``NativeProviderUnavailableError`` -> ``NATIVE_UNAVAILABLE`` (never a
      success status — no algorithmic fallback occurs; this only changes
      how the "no native dependency" fact is surfaced).
    * ``CanonicalProjectionError`` (invalid input — e.g. duplicate entity
      ids, malformed endpoints) -> ``FAILED``, with the validation failure
      recorded verbatim in ``reason``.
    * ``DerivedHierarchyConfigError`` (malformed build configuration —
      incoherent mixed-domain input, invalid ``max_cluster_size``/``seed``,
      or a structurally invalid evidence sidecar) -> ``FAILED``, with the
      validation failure recorded verbatim in ``reason``.
    * ``DerivedHierarchyBuildError`` (provider output could not be
      reconciled into a valid tree, including failing post-build
      structural/semantic validation) -> ``FAILED``, with the structural
      inconsistency recorded verbatim in ``reason``.

    Callers that prefer exceptions for invalid input/provider-output
    failures (as opposed to native-availability, which is expected to be
    environment-dependent) should call ``build_derived_community_hierarchy``
    directly instead."""
    try:
        return build_derived_community_hierarchy(
            entities,
            relationships,
            provider=provider,
            policy_id=policy_id,
            run_id=run_id,
            config_id=config_id,
            max_cluster_size=max_cluster_size,
            seed=seed,
            entity_evidence=entity_evidence,
            relationship_evidence=relationship_evidence,
        )
    except NativeProviderUnavailableError as exc:
        return DerivedHierarchyOutcome(
            status=HierarchyStatus.NATIVE_UNAVAILABLE,
            reason=str(exc),
            provider_id=getattr(provider, "provider_id", None),
            provider_version=getattr(provider, "provider_version", None),
        )
    except (
        CanonicalProjectionError,
        DerivedHierarchyConfigError,
        DerivedHierarchyBuildError,
    ) as exc:
        return DerivedHierarchyOutcome(
            status=HierarchyStatus.FAILED,
            reason=str(exc),
            provider_id=getattr(provider, "provider_id", None),
            provider_version=getattr(provider, "provider_version", None),
        )


def _attach_relationship_evidence(
    clusters: list[DerivedClusterRow],
    memberships: list[DerivedMembershipRow],
    *,
    projection: CanonicalProjection,
    relationships: list[RelationshipRow],
    relationship_evidence: dict[str, tuple[str, ...]],
) -> tuple[list[DerivedClusterRow], tuple[InterCommunityRelationshipDiagnostic, ...]]:
    """Fold relationship-sourced evidence into terminal clusters and
    re-propagate it bottom-up to every ancestor, without rewriting the
    typed/directed ``RelationshipRow`` objects or per-entity membership
    evidence (this is sidecar enrichment of hierarchy artifacts only).

    * Self-loop evidence is attributed to the self-looping entity's own
      terminal cluster (self-loops are already excluded from the
      canonical partition graph, but their evidence still belongs to the
      owning entity's community).
    * Each projection edge's contributing relationships' evidence is
      attributed to BOTH endpoints' terminal clusters. When both
      endpoints share a terminal cluster this is a no-op duplicate (set
      union); when they differ, evidence is attributed to both sides
      (never dropped, never picked arbitrarily) and an
      ``InterCommunityRelationshipDiagnostic`` records the dual
      attribution explicitly.
    """
    entity_to_cluster: dict[str, str] = {m.entity_id: m.cluster_id for m in memberships}
    relationship_by_id = {r.relationship_id: r for r in relationships}

    extra_evidence_by_cluster: dict[str, set[str]] = {}
    diagnostics: list[InterCommunityRelationshipDiagnostic] = []

    def _add(cluster_id: Optional[str], ids: tuple[str, ...]) -> None:
        if not cluster_id or not ids:
            return
        extra_evidence_by_cluster.setdefault(cluster_id, set()).update(ids)

    for self_loop in projection.self_loop_diagnostics:
        ids = relationship_evidence.get(self_loop.relationship_id, ())
        _add(entity_to_cluster.get(self_loop.entity_id), ids)

    for edge in projection.edges:
        source_cluster = entity_to_cluster.get(edge.source_entity_id)
        target_cluster = entity_to_cluster.get(edge.target_entity_id)
        for relationship_id in edge.contributing_relationship_ids:
            ids = relationship_evidence.get(relationship_id, ())
            _add(source_cluster, ids)
            _add(target_cluster, ids)
            if (
                source_cluster is not None
                and target_cluster is not None
                and source_cluster != target_cluster
            ):
                rel = relationship_by_id.get(relationship_id)
                if rel is None:
                    # Should be unreachable: every contributing_relationship_id
                    # on a canonical edge is, by construction, one of the
                    # RelationshipRow objects passed in. Skip rather than
                    # fabricate a diagnostic with unknown/empty endpoints.
                    continue
                # ``edge`` holds the undirected *sorted* endpoint pair used
                # for partition-graph weighting; it is NOT necessarily the
                # relationship's own directed source/target (a reciprocal or
                # lexically-backward relationship can have its original
                # source/target swapped relative to the sorted pair). Always
                # report the relationship's own directed endpoints, with
                # each endpoint's cluster id resolved independently of
                # whichever side ``edge`` happened to sort it onto.
                if rel.source_entity_id == edge.source_entity_id:
                    rel_source_cluster, rel_target_cluster = source_cluster, target_cluster
                else:
                    rel_source_cluster, rel_target_cluster = target_cluster, source_cluster
                diagnostics.append(
                    InterCommunityRelationshipDiagnostic(
                        relationship_id=relationship_id,
                        relationship_type=rel.relationship_type,
                        source_entity_id=rel.source_entity_id,
                        target_entity_id=rel.target_entity_id,
                        source_cluster_id=rel_source_cluster,
                        target_cluster_id=rel_target_cluster,
                    )
                )

    if not extra_evidence_by_cluster:
        updated_clusters = clusters
    else:
        updated_clusters = []
        for cluster in clusters:
            extra = extra_evidence_by_cluster.get(cluster.cluster_id)
            if not extra:
                updated_clusters.append(cluster)
                continue
            relationship_contributed = tuple(
                sorted(set(cluster.relationship_contributed_evidence_ids) | extra)
            )
            new_evidence = tuple(sorted(set(cluster.evidence_ids) | extra))
            updated_clusters.append(
                replace(
                    cluster,
                    relationship_contributed_evidence_ids=relationship_contributed,
                    evidence_ids=new_evidence,
                )
            )

    updated_clusters = _repropagate_evidence(updated_clusters)
    return updated_clusters, tuple(diagnostics)


def _repropagate_evidence(clusters: list[DerivedClusterRow]) -> list[DerivedClusterRow]:
    """Recompute every internal (non-terminal) cluster's ``evidence_ids`` as
    the union of its direct children's (possibly just-updated)
    ``evidence_ids``, processing bottom-up by ascending ``structural_height``
    so every child is finalized before its parent is recomputed. Terminal
    clusters (height 0) are left untouched here — their evidence was
    already finalized by relationship-evidence attribution (or member
    evidence alone, if no relationship evidence applied to them)."""
    by_id = {c.cluster_id: c for c in clusters}
    children_by_parent: dict[str, list[str]] = {}
    for c in clusters:
        if c.parent_cluster_id is not None:
            children_by_parent.setdefault(c.parent_cluster_id, []).append(c.cluster_id)

    for cluster in sorted(clusters, key=lambda c: c.structural_height):
        if cluster.is_terminal:
            continue
        child_ids = children_by_parent.get(cluster.cluster_id, ())
        union_evidence = sorted({e for cid in child_ids for e in by_id[cid].evidence_ids})
        by_id[cluster.cluster_id] = replace(
            by_id[cluster.cluster_id], evidence_ids=tuple(union_evidence)
        )

    return [by_id[c.cluster_id] for c in clusters]


def _build_singleton_terminals(
    node_ids: tuple[str, ...],
    entity_evidence: dict[str, tuple[str, ...]],
    *,
    provider: CommunityProvider,
    policy_id: str,
    domain_hash: Optional[str],
    run_id: str,
    projection: CanonicalProjection,
    config_id: str,
    seed: int,
) -> tuple[list[DerivedClusterRow], list[DerivedMembershipRow]]:
    """One singleton terminal cluster (height 0, no native depth, no
    parent) per node id, used for isolates and for the fully-insufficient
    (edgeless) branch alike."""
    del projection, config_id, seed  # reserved for future per-call context; unused here
    clusters: list[DerivedClusterRow] = []
    memberships: list[DerivedMembershipRow] = []
    for node_id in sorted(node_ids):
        evidence = entity_evidence.get(node_id, ())
        cluster_id = compute_community_identity(
            domain_hash=domain_hash,
            provider_id=provider.provider_id,
            provider_version=provider.provider_version,
            policy_id=policy_id,
            member_entity_ids=(node_id,),
            structural_lineage=(),
        )
        clusters.append(
            DerivedClusterRow(
                cluster_id=cluster_id,
                hierarchy_version=DERIVED_HIERARCHY_VERSION,
                parent_cluster_id=None,
                structural_height=0,
                native_depth=None,
                is_terminal=True,
                is_singleton_isolate=True,
                is_oversized=False,
                member_entity_ids=(node_id,),
                evidence_ids=evidence,
                provider_id=provider.provider_id,
                provider_version=provider.provider_version,
                policy_id=policy_id,
                domain_hash=domain_hash,
                run_id=run_id,
                execution_fingerprint="",  # filled in by caller once computed
            )
        )
        memberships.append(
            DerivedMembershipRow(
                cluster_id=cluster_id,
                entity_id=node_id,
                evidence_ids=evidence,
            )
        )
    return clusters, memberships


def _build_tree_from_entries(
    entries: list[NativePartitionEntry],
    entity_evidence: dict[str, tuple[str, ...]],
    *,
    provider: CommunityProvider,
    policy_id: str,
    domain_hash: Optional[str],
    run_id: str,
    max_cluster_size: int,
    expected_node_ids: frozenset[str],
) -> tuple[list[DerivedClusterRow], list[DerivedMembershipRow], tuple[str, ...]]:
    """Reconcile the provider's flat partition-entry list into a validated
    tree of ``DerivedClusterRow``/``DerivedMembershipRow`` plus the set of
    oversized terminal cluster identity strings.

    ``expected_node_ids`` is the exact set of connected (non-isolate)
    entity ids submitted to the provider via the native edge list. The
    provider's output is untrusted and is validated against it here:
    an unknown node id in a final-cluster row, a dropped/missing expected
    node id, a parent reference to a cluster id that never itself appears
    as an entry, or a cluster id unreachable from any root (a disjoint or
    purely-cyclic sub-structure the native output can produce) are all
    rejected explicitly — never silently accepted or left to surface as an
    opaque ``KeyError`` once the tree is half-built."""
    if not entries:
        if expected_node_ids:
            raise DerivedHierarchyBuildError(
                "Provider returned no partition entries but "
                f"{len(expected_node_ids)} connected entity id(s) were submitted "
                "for partitioning; provider output does not conserve input nodes."
            )
        return [], [], ()

    cluster_parent: dict[int, Optional[int]] = {}
    cluster_level: dict[int, int] = {}
    direct_members: dict[int, set[str]] = {}
    has_non_final_rows: dict[int, bool] = {}

    for entry in entries:
        cid = entry.cluster_id
        # Validated for every row, final or not: a non-final row's node_id
        # still names the entity being tracked through an intermediate
        # (internal) level, and an unknown id there is just as untrusted as
        # one on a final-cluster row — it must never be silently discarded.
        if entry.node_id not in expected_node_ids:
            raise DerivedHierarchyBuildError(
                f"Provider output assigns node id {entry.node_id!r} to a "
                f"{'final' if entry.is_final_cluster else 'non-final'} "
                "cluster, but this node id was not among the connected entity "
                "id(s) submitted for partitioning; refusing to accept an "
                "unknown native node."
            )
        parent = entry.parent_cluster_id if entry.parent_cluster_id not in (-1,) else None
        if cid in cluster_parent and cluster_parent[cid] != parent:
            raise DerivedHierarchyBuildError(
                f"Native cluster id {cid} reported inconsistent parent values "
                f"({cluster_parent[cid]!r} vs {parent!r}) across partition entries; "
                "cannot build a well-formed tree from this provider output."
            )
        cluster_parent[cid] = parent
        if cid in cluster_level and cluster_level[cid] != entry.level:
            raise DerivedHierarchyBuildError(
                f"Native cluster id {cid} reported inconsistent level values "
                f"({cluster_level[cid]!r} vs {entry.level!r}) across partition entries."
            )
        cluster_level[cid] = entry.level
        if entry.is_final_cluster:
            direct_members.setdefault(cid, set()).add(entry.node_id)
        else:
            has_non_final_rows[cid] = True

    for cid in direct_members:
        if has_non_final_rows.get(cid):
            raise DerivedHierarchyBuildError(
                f"Native cluster id {cid} has both direct final-cluster members "
                "and non-final rows; the terminal-vs-internal assumption this "
                "builder relies on does not hold for this provider output."
            )

    all_native_members: set[str] = set().union(*direct_members.values()) if direct_members else set()
    missing_members = expected_node_ids - all_native_members
    if missing_members:
        raise DerivedHierarchyBuildError(
            f"Provider output is missing {len(missing_members)} connected "
            f"entity id(s) that were submitted for partitioning (e.g. "
            f"{sorted(missing_members)[:5]!r}); provider output does not "
            "conserve input nodes."
        )

    all_cids = set(cluster_parent.keys())
    dangling_parents = {p for p in cluster_parent.values() if p is not None} - all_cids
    if dangling_parents:
        raise DerivedHierarchyBuildError(
            f"Provider output references parent cluster id(s) "
            f"{sorted(dangling_parents)!r} that never themselves appear as a "
            "cluster_id in any partition entry."
        )

    children: dict[Optional[int], list[int]] = {}
    for cid, parent in cluster_parent.items():
        children.setdefault(parent, []).append(cid)

    roots = sorted(children.get(None, []))
    if not roots:
        raise DerivedHierarchyBuildError(
            "Provider output has no root-level clusters (no entry with a null "
            "parent_cluster_id); cannot build a tree."
        )

    identity_by_cid: dict[int, str] = {}
    members_by_cid: dict[int, tuple[str, ...]] = {}
    evidence_by_cid: dict[int, tuple[str, ...]] = {}
    height_by_cid: dict[int, int] = {}
    clusters: list[DerivedClusterRow] = []
    memberships: list[DerivedMembershipRow] = []
    oversized_ids: list[str] = []

    visiting: set[int] = set()
    visited_cids: set[int] = set()

    def visit(cid: int) -> None:
        visited_cids.add(cid)
        if cid in visiting:
            raise DerivedHierarchyBuildError(
                f"Cycle detected in native cluster parent links at cluster id {cid}."
            )
        visiting.add(cid)
        child_cids = sorted(children.get(cid, []))
        for child_cid in child_cids:
            visit(child_cid)  # own identity resolved below; parent link set in second pass
        visiting.discard(cid)

        is_terminal = cid in direct_members
        if is_terminal and child_cids:
            raise DerivedHierarchyBuildError(
                f"Native cluster id {cid} has both direct final-cluster members "
                "and child clusters; cannot classify it as purely terminal or "
                "purely internal."
            )

        if is_terminal:
            members = tuple(sorted(direct_members[cid]))
            lineage: tuple[str, ...] = ()
            evidence = tuple(sorted({e for m in members for e in entity_evidence.get(m, ())}))
            height = 0
        else:
            members = tuple(sorted({m for c in child_cids for m in members_by_cid[c]}))
            lineage = tuple(sorted(identity_by_cid[c] for c in child_cids))
            evidence = tuple(sorted({e for c in child_cids for e in evidence_by_cid[c]}))
            height = 1 + max((height_by_cid[c] for c in child_cids), default=0)

        cluster_identity = compute_community_identity(
            domain_hash=domain_hash,
            provider_id=provider.provider_id,
            provider_version=provider.provider_version,
            policy_id=policy_id,
            member_entity_ids=members,
            structural_lineage=lineage,
        )
        identity_by_cid[cid] = cluster_identity
        members_by_cid[cid] = members
        evidence_by_cid[cid] = evidence
        height_by_cid[cid] = height

        is_oversized = is_terminal and len(members) > max_cluster_size
        if is_oversized:
            oversized_ids.append(cluster_identity)

        clusters.append(
            DerivedClusterRow(
                cluster_id=cluster_identity,
                hierarchy_version=DERIVED_HIERARCHY_VERSION,
                parent_cluster_id=None,  # second pass fills this in below
                structural_height=height,
                native_depth=cluster_level[cid],
                is_terminal=is_terminal,
                is_singleton_isolate=False,
                is_oversized=is_oversized,
                member_entity_ids=members,
                evidence_ids=evidence,
                provider_id=provider.provider_id,
                provider_version=provider.provider_version,
                policy_id=policy_id,
                domain_hash=domain_hash,
                run_id=run_id,
                execution_fingerprint="",  # filled in by caller once computed
            )
        )
        if is_terminal:
            for member in members:
                memberships.append(
                    DerivedMembershipRow(
                        cluster_id=cluster_identity,
                        entity_id=member,
                        evidence_ids=tuple(entity_evidence.get(member, ())),
                    )
                )

    for root_cid in roots:
        visit(root_cid)

    unreached = all_cids - visited_cids
    if unreached:
        raise DerivedHierarchyBuildError(
            f"Native cluster id(s) {sorted(unreached)!r} are not reachable from "
            "any root-level cluster (a disjoint or purely-cyclic sub-structure "
            "in the provider output); every reported cluster must descend from "
            "a root for its members and lineage to conserve the input graph."
        )

    # Second pass: now that every cluster's own identity is known, set each
    # row's parent_cluster_id to the PARENT's already-computed identity
    # string (never a native integer id).
    index_by_identity = {c.cluster_id: i for i, c in enumerate(clusters)}
    for cid, parent_cid in cluster_parent.items():
        if parent_cid is None:
            continue
        own_identity = identity_by_cid[cid]
        parent_identity = identity_by_cid[parent_cid]
        idx = index_by_identity[own_identity]
        clusters[idx] = replace(clusters[idx], parent_cluster_id=parent_identity)

    return clusters, memberships, tuple(oversized_ids)
