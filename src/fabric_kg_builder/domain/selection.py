"""Deterministic relationship-vocabulary and question-path selection."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import fsum
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .proposal import (
        ProposalQuestionRoute,
        RelationshipCandidate,
    )


class ProposalSelectionError(ValueError):
    """Raised when candidate relationships cannot produce a bounded proposal."""


@dataclass(frozen=True)
class SelectedPathStep:
    """One directed traversal through a selected relationship candidate."""

    from_type: str
    relationship_type: str
    to_type: str
    traversal: str


@dataclass(frozen=True)
class SelectedQuestionPlan:
    """Locally computed shortest path or explicit unsupported result."""

    question_id: str
    required_path: tuple[SelectedPathStep, ...]
    covered: bool
    unsupported_reason: str | None = None

    @property
    def hop_count(self) -> int:
        return len(self.required_path)


@dataclass(frozen=True)
class SelectionResult:
    """Deterministically selected vocabulary and question plans."""

    relationships: tuple["RelationshipCandidate", ...]
    question_plans: tuple[SelectedQuestionPlan, ...]
    max_hops: int
    max_hops_rationale: str | None
    relationship_type_count_rationale: str | None
    merge_groups: dict[str, tuple[str, ...]]


def _score(candidate: "RelationshipCandidate") -> float:
    scores = candidate.scores
    return fsum(
        (
            scores.coverage_score,
            scores.source_support_score,
            scores.reuse_score,
            scores.clarity_score,
            -scores.risk_penalty,
            -scores.redundancy_penalty,
        )
    )


def _same_signature(
    left: "RelationshipCandidate",
    right: "RelationshipCandidate",
) -> bool:
    return (
        tuple(sorted(left.source_types)) == tuple(sorted(right.source_types))
        and tuple(sorted(left.target_types)) == tuple(sorted(right.target_types))
    )


def _inverse_signature(
    left: "RelationshipCandidate",
    right: "RelationshipCandidate",
) -> bool:
    return (
        tuple(sorted(left.source_types)) == tuple(sorted(right.target_types))
        and tuple(sorted(left.target_types)) == tuple(sorted(right.source_types))
    )


def _can_merge(
    left: "RelationshipCandidate",
    right: "RelationshipCandidate",
) -> bool:
    if left.semantic_key != right.semantic_key:
        return False
    if left.endpoint_policy != right.endpoint_policy:
        return False
    if _same_signature(left, right):
        return True
    inverse_declared = left.inverse_of == right.id or right.inverse_of == left.id
    return inverse_declared and _inverse_signature(left, right)


def merge_relationship_candidates(
    candidates: Iterable["RelationshipCandidate"],
) -> tuple[
    list["RelationshipCandidate"],
    dict[str, str],
    dict[str, tuple[str, ...]],
]:
    """Merge only explicitly contract-safe duplicates and inverses.

    The lexicographically smallest candidate ID is the canonical direction. An
    inverse candidate is translated into that direction; competency-question
    and evidence support are unioned.
    """

    ordered = sorted(candidates, key=lambda item: item.id)
    by_id = {item.id: item for item in ordered}
    if len(by_id) != len(ordered):
        raise ProposalSelectionError("Duplicate relationship candidate IDs are not allowed.")

    groups: list[list["RelationshipCandidate"]] = []
    assigned: set[str] = set()
    for candidate in ordered:
        if candidate.id in assigned:
            continue
        group = [candidate]
        assigned.add(candidate.id)
        changed = True
        while changed:
            changed = False
            for other in ordered:
                if other.id in assigned:
                    continue
                if any(_can_merge(member, other) for member in group):
                    group.append(other)
                    assigned.add(other.id)
                    changed = True
        groups.append(sorted(group, key=lambda item: item.id))

    merged: list["RelationshipCandidate"] = []
    aliases: dict[str, str] = {}
    merge_groups: dict[str, tuple[str, ...]] = {}
    for group in groups:
        representative = group[0]
        canonical_id = representative.id
        aliases.update({item.id: canonical_id for item in group})
        merge_groups[canonical_id] = tuple(item.id for item in group)
        if len(group) == 1:
            merged.append(representative)
            continue

        question_ids = sorted(
            {
                question_id
                for item in group
                for question_id in item.competency_question_ids
            }
        )
        evidence_ids = sorted(
            {
                evidence_id
                for item in group
                for evidence_id in item.source_evidence_ids
            }
        )
        governance_rules = sorted(
            {
                item.governance_rule
                for item in group
                if item.governance_rule
            }
        )
        strongest = max(group, key=lambda item: (_score(item), item.id))
        merged.append(
            representative.model_copy(
                update={
                    "description": strongest.description,
                    "competency_question_ids": question_ids,
                    "source_evidence_ids": evidence_ids,
                    "governance_rule": (
                        " ".join(governance_rules) if governance_rules else None
                    ),
                    "inverse_of": None,
                    "scores": strongest.scores,
                }
            )
        )

    return sorted(merged, key=lambda item: item.id), aliases, merge_groups


def _adjacency(
    relationships: Iterable["RelationshipCandidate"],
    question_id: str,
) -> dict[str, list[SelectedPathStep]]:
    adjacency: dict[str, list[SelectedPathStep]] = {}
    for relationship in sorted(relationships, key=lambda item: item.id):
        if question_id not in relationship.competency_question_ids:
            continue
        for source_type in sorted(relationship.source_types):
            for target_type in sorted(relationship.target_types):
                adjacency.setdefault(source_type, []).append(
                    SelectedPathStep(
                        from_type=source_type,
                        relationship_type=relationship.id,
                        to_type=target_type,
                        traversal="forward",
                    )
                )
                adjacency.setdefault(target_type, []).append(
                    SelectedPathStep(
                        from_type=target_type,
                        relationship_type=relationship.id,
                        to_type=source_type,
                        traversal="reverse",
                    )
                )
    for steps in adjacency.values():
        steps.sort(
            key=lambda item: (
                item.relationship_type,
                item.traversal,
                item.to_type,
            )
        )
    return adjacency


def _enumerate_paths(
    route: "ProposalQuestionRoute",
    relationships: Iterable["RelationshipCandidate"],
    *,
    max_hops: int,
    max_options: int = 4096,
) -> list[tuple[SelectedPathStep, ...]]:
    if route.start_type is None or route.end_type is None:
        return []
    adjacency = _adjacency(relationships, route.question_id)
    queue: deque[
        tuple[str, tuple[SelectedPathStep, ...], frozenset[str]]
    ] = deque(
        [(route.start_type, tuple(), frozenset({route.start_type}))]
    )
    paths: list[tuple[SelectedPathStep, ...]] = []
    seen_signatures: set[tuple[tuple[str, str], ...]] = set()
    while queue:
        node, path, visited = queue.popleft()
        if len(path) >= max_hops:
            continue
        for step in adjacency.get(node, []):
            next_path = (*path, step)
            if step.to_type == route.end_type:
                signature = tuple(
                    (item.relationship_type, item.traversal)
                    for item in next_path
                )
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    paths.append(next_path)
                    if len(paths) > max_options:
                        raise ProposalSelectionError(
                            f"Question '{route.question_id}' produced more than "
                            f"{max_options} bounded path options; ask Copilot for a "
                            "smaller candidate vocabulary."
                        )
                continue
            if step.to_type in visited:
                continue
            queue.append(
                (
                    step.to_type,
                    next_path,
                    visited | {step.to_type},
                )
            )
    return sorted(
        paths,
        key=lambda path: (
            len(path),
            tuple(
                (step.relationship_type, step.traversal, step.to_type)
                for step in path
            ),
        ),
    )


def _shortest_path(
    route: "ProposalQuestionRoute",
    relationships: Iterable["RelationshipCandidate"],
) -> tuple[SelectedPathStep, ...] | None:
    paths = _enumerate_paths(route, relationships, max_hops=4)
    return paths[0] if paths else None


def _selection_key(
    relationship_ids: frozenset[str],
    by_id: dict[str, "RelationshipCandidate"],
) -> tuple[int, float, tuple[str, ...]]:
    stable_ids = tuple(sorted(relationship_ids))
    return (
        len(relationship_ids),
        -fsum(_score(by_id[item_id]) for item_id in stable_ids),
        stable_ids,
    )


def _prune_dominated(
    states: set[frozenset[str]],
    by_id: dict[str, "RelationshipCandidate"],
) -> set[frozenset[str]]:
    ordered = sorted(states, key=lambda item: _selection_key(item, by_id))
    kept: list[frozenset[str]] = []
    for state in ordered:
        if any(existing <= state for existing in kept):
            continue
        kept.append(state)
    return set(kept)


def select_relationship_vocabulary(
    candidates: Iterable["RelationshipCandidate"],
    routes: Iterable["ProposalQuestionRoute"],
    *,
    critical_question_ids: set[str],
) -> SelectionResult:
    """Choose the minimum bounded vocabulary and derive question-scoped K."""

    merged, _aliases, merge_groups = merge_relationship_candidates(candidates)
    by_id = {item.id: item for item in merged}
    mandatory = frozenset(
        item.id for item in merged if item.governance_rule
    )
    route_list = sorted(routes, key=lambda item: item.question_id)
    states: set[frozenset[str]] = {mandatory}
    unsupported: dict[str, str] = {}

    for route in route_list:
        options = _enumerate_paths(route, merged, max_hops=4)
        if not options:
            unsupported[route.question_id] = (
                route.unsupported_reason
                or "No source-supported typed path of four or fewer hops was found; "
                "split the question into bounded subquestions."
            )
            continue
        option_ids = {
            frozenset(step.relationship_type for step in path)
            for path in options
        }
        next_states = {
            state | option
            for state in states
            for option in option_ids
        }
        states = _prune_dominated(next_states, by_id)

    if not states:
        raise ProposalSelectionError("No relationship vocabulary covers the proposed routes.")
    selected_ids = min(states, key=lambda item: _selection_key(item, by_id))
    if len(selected_ids) > 24:
        raise ProposalSelectionError(
            f"[DOM-103] Minimal relationship vocabulary requires N={len(selected_ids)}, "
            "which exceeds the hard maximum of 24."
        )

    selected = tuple(by_id[item_id] for item_id in sorted(selected_ids))
    plans: list[SelectedQuestionPlan] = []
    for route in route_list:
        path = _shortest_path(route, selected)
        if path is None:
            plans.append(
                SelectedQuestionPlan(
                    question_id=route.question_id,
                    required_path=tuple(),
                    covered=False,
                    unsupported_reason=unsupported.get(
                        route.question_id,
                        route.unsupported_reason
                        or "The minimal selected vocabulary does not support this question.",
                    ),
                )
            )
        else:
            plans.append(
                SelectedQuestionPlan(
                    question_id=route.question_id,
                    required_path=path,
                    covered=True,
                )
            )

    covered_plans = [plan for plan in plans if plan.covered]
    if not covered_plans:
        raise ProposalSelectionError(
            "[DOM-104] At least one competency question must be covered."
        )
    max_hops = max(plan.hop_count for plan in covered_plans)
    max_hops_rationale: str | None = None
    if max_hops == 4:
        four_hop_plans = [plan for plan in covered_plans if plan.hop_count == 4]
        relationship_map = {item.id: item for item in selected}
        for plan in four_hop_plans:
            if any(
                not relationship_map[step.relationship_type].source_evidence_ids
                for step in plan.required_path
            ):
                raise ProposalSelectionError(
                    f"[DOM-105] K=4 path for '{plan.question_id}' requires source "
                    "evidence on all four relationships."
                )
        max_hops_rationale = (
            "K=4 is required by the cited shortest path(s) for "
            + ", ".join(plan.question_id for plan in four_hop_plans)
            + "."
        )

    count_rationale = None
    if len(selected) > 20:
        count_rationale = (
            f"N={len(selected)} is the deterministic minimum needed to cover all "
            "source-supported competency-question paths and governance rules."
        )

    # Unsupported critical questions remain in the result; deterministic domain
    # validation will block approval while still preserving the proposal artifact.
    _ = critical_question_ids & set(unsupported)
    return SelectionResult(
        relationships=selected,
        question_plans=tuple(plans),
        max_hops=max_hops,
        max_hops_rationale=max_hops_rationale,
        relationship_type_count_rationale=count_rationale,
        merge_groups=merge_groups,
    )
