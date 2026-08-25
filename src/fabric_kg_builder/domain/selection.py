"""Deterministic candidate merge and minimum CQ-covering relationship selection."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import fsum
from typing import Iterable

from .proposal import ProposalQuestionRouteV2, RelationshipCandidateV2

SELECTOR_VERSION = "l1-domain-selector/1.0.0"


class ProposalSelectionError(ValueError):
    """Raised when candidates cannot produce a bounded valid vocabulary."""


@dataclass(frozen=True)
class SelectedPathStep:
    from_type_id: str
    relationship_type_id: str
    to_type_id: str
    traversal: str
    evidence_span_ids: tuple[str, ...]


@dataclass(frozen=True)
class SelectedQuestionPlan:
    question_id: str
    required_path: tuple[SelectedPathStep, ...]
    covered: bool
    unsupported_reason: str | None = None

    @property
    def hop_count(self) -> int:
        return len(self.required_path)


@dataclass(frozen=True)
class SelectionResult:
    relationships: tuple[RelationshipCandidateV2, ...]
    question_plans: tuple[SelectedQuestionPlan, ...]
    max_hops: int
    retained_type_rationales: dict[str, tuple[str, ...]]
    merge_groups: dict[str, tuple[str, ...]]


def _score(candidate: RelationshipCandidateV2) -> float:
    return candidate.score.total_score


def _same_signature(
    left: RelationshipCandidateV2,
    right: RelationshipCandidateV2,
) -> bool:
    return (
        left.source_type_ids == right.source_type_ids
        and left.target_type_ids == right.target_type_ids
    )


def _inverse_signature(
    left: RelationshipCandidateV2,
    right: RelationshipCandidateV2,
) -> bool:
    return (
        left.source_type_ids == right.target_type_ids
        and left.target_type_ids == right.source_type_ids
    )


def _can_merge(
    left: RelationshipCandidateV2,
    right: RelationshipCandidateV2,
) -> bool:
    if left.semantic_key != right.semantic_key:
        return False
    if left.endpoint_policy != right.endpoint_policy:
        return False
    if _same_signature(left, right):
        return True
    inverse_declared = (
        left.inverse_of_candidate_id == right.candidate_id
        or right.inverse_of_candidate_id == left.candidate_id
    )
    return inverse_declared and _inverse_signature(left, right)


def merge_relationship_candidates(
    candidates: Iterable[RelationshipCandidateV2],
) -> tuple[
    list[RelationshipCandidateV2],
    dict[str, str],
    dict[str, tuple[str, ...]],
]:
    """Merge only identical signatures or explicit exact inverses."""
    ordered = sorted(candidates, key=lambda item: item.candidate_id)
    candidate_ids = [item.candidate_id for item in ordered]
    relationship_ids = [item.relationship_type_id for item in ordered]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ProposalSelectionError("duplicate relationship candidate IDs")
    if len(relationship_ids) != len(set(relationship_ids)):
        raise ProposalSelectionError("duplicate proposed relationship type IDs")

    groups: list[list[RelationshipCandidateV2]] = []
    assigned: set[str] = set()
    for candidate in ordered:
        if candidate.candidate_id in assigned:
            continue
        group = [candidate]
        assigned.add(candidate.candidate_id)
        changed = True
        while changed:
            changed = False
            for other in ordered:
                if other.candidate_id in assigned:
                    continue
                if any(_can_merge(member, other) for member in group):
                    group.append(other)
                    assigned.add(other.candidate_id)
                    changed = True
        groups.append(
            sorted(
                group,
                key=lambda item: (
                    item.relationship_type_id,
                    item.candidate_id,
                ),
            )
        )

    merged: list[RelationshipCandidateV2] = []
    aliases: dict[str, str] = {}
    merge_groups: dict[str, tuple[str, ...]] = {}
    for group in groups:
        representative = group[0]
        representative_id = representative.candidate_id
        aliases.update(
            {item.candidate_id: representative_id for item in group}
        )
        merge_groups[representative_id] = tuple(
            item.candidate_id for item in group
        )
        if len(group) == 1:
            merged.append(representative)
            continue
        strongest = min(
            group,
            key=lambda item: (-_score(item), item.candidate_id),
        )
        governance = sorted(
            {
                item.governance_rationale
                for item in group
                if item.governance_rationale is not None
            }
        )
        merged.append(
            representative.model_copy(
                update={
                    "description": strongest.description,
                    "competency_question_ids": tuple(
                        sorted(
                            {
                                question_id
                                for item in group
                                for question_id in item.competency_question_ids
                            }
                        )
                    ),
                    "evidence_span_ids": tuple(
                        sorted(
                            {
                                evidence_id
                                for item in group
                                for evidence_id in item.evidence_span_ids
                            }
                        )
                    ),
                    "governance_rationale": (
                        " ".join(governance) if governance else None
                    ),
                    "inverse_of_candidate_id": None,
                    "score_inputs": strongest.score_inputs,
                    "score": strongest.score,
                }
            )
        )
    return (
        sorted(merged, key=lambda item: item.relationship_type_id),
        aliases,
        merge_groups,
    )


def _adjacency(
    relationships: Iterable[RelationshipCandidateV2],
    question_id: str,
) -> dict[str, list[SelectedPathStep]]:
    adjacency: dict[str, list[SelectedPathStep]] = {}
    for relationship in sorted(
        relationships, key=lambda item: item.relationship_type_id
    ):
        if question_id not in relationship.competency_question_ids:
            continue
        for source_type_id in relationship.source_type_ids:
            for target_type_id in relationship.target_type_ids:
                adjacency.setdefault(source_type_id, []).append(
                    SelectedPathStep(
                        from_type_id=source_type_id,
                        relationship_type_id=relationship.relationship_type_id,
                        to_type_id=target_type_id,
                        traversal="forward",
                        evidence_span_ids=relationship.evidence_span_ids,
                    )
                )
                adjacency.setdefault(target_type_id, []).append(
                    SelectedPathStep(
                        from_type_id=target_type_id,
                        relationship_type_id=relationship.relationship_type_id,
                        to_type_id=source_type_id,
                        traversal="reverse",
                        evidence_span_ids=relationship.evidence_span_ids,
                    )
                )
    for steps in adjacency.values():
        steps.sort(
            key=lambda item: (
                item.relationship_type_id,
                item.traversal,
                item.to_type_id,
            )
        )
    return adjacency


def _enumerate_paths(
    route: ProposalQuestionRouteV2,
    relationships: Iterable[RelationshipCandidateV2],
    *,
    max_hops: int,
    max_options: int = 4096,
) -> list[tuple[SelectedPathStep, ...]]:
    if route.start_type_id is None or route.end_type_id is None:
        return []
    adjacency = _adjacency(relationships, route.question_id)
    queue: deque[
        tuple[str, tuple[SelectedPathStep, ...], frozenset[str]]
    ] = deque([(route.start_type_id, (), frozenset({route.start_type_id}))])
    paths: list[tuple[SelectedPathStep, ...]] = []
    signatures: set[tuple[tuple[str, str], ...]] = set()
    while queue:
        node, path, visited = queue.popleft()
        if len(path) >= max_hops:
            continue
        for step in adjacency.get(node, []):
            next_path = (*path, step)
            if step.to_type_id == route.end_type_id:
                signature = tuple(
                    (item.relationship_type_id, item.traversal)
                    for item in next_path
                )
                if signature not in signatures:
                    signatures.add(signature)
                    paths.append(next_path)
                    if len(paths) > max_options:
                        raise ProposalSelectionError(
                            f"question {route.question_id} exceeded {max_options} paths"
                        )
                continue
            if step.to_type_id in visited:
                continue
            queue.append(
                (step.to_type_id, next_path, visited | {step.to_type_id})
            )
    return sorted(
        paths,
        key=lambda path: (
            len(path),
            tuple(
                (
                    step.relationship_type_id,
                    step.traversal,
                    step.to_type_id,
                )
                for step in path
            ),
        ),
    )


def _selection_key(
    relationship_ids: frozenset[str],
    by_id: dict[str, RelationshipCandidateV2],
) -> tuple[int, float, tuple[str, ...]]:
    stable_ids = tuple(sorted(relationship_ids))
    return (
        len(stable_ids),
        -fsum(_score(by_id[item_id]) for item_id in stable_ids),
        stable_ids,
    )


def _prune_dominated(
    states: set[frozenset[str]],
    by_id: dict[str, RelationshipCandidateV2],
) -> set[frozenset[str]]:
    kept: list[frozenset[str]] = []
    for state in sorted(states, key=lambda item: _selection_key(item, by_id)):
        if any(existing <= state for existing in kept):
            continue
        kept.append(state)
    return set(kept)


def select_relationship_vocabulary(
    candidates: Iterable[RelationshipCandidateV2],
    routes: Iterable[ProposalQuestionRouteV2],
    *,
    critical_question_ids: set[str],
    required_relationship_type_ids: set[str] | None = None,
) -> SelectionResult:
    """Select the minimum path union plus mandatory governance/role relationships."""
    merged, _aliases, merge_groups = merge_relationship_candidates(candidates)
    if any(not item.score.ip_governance_eligible for item in merged):
        merged = [item for item in merged if item.score.ip_governance_eligible]
    by_id = {item.relationship_type_id: item for item in merged}
    mandatory = {
        item.relationship_type_id
        for item in merged
        if item.governance_rationale is not None
    }
    mandatory.update(required_relationship_type_ids or set())
    unknown_mandatory = mandatory - set(by_id)
    if unknown_mandatory:
        raise ProposalSelectionError(
            f"required relationship types are unavailable: {sorted(unknown_mandatory)}"
        )

    route_list = sorted(routes, key=lambda item: item.question_id)
    states: set[frozenset[str]] = {frozenset(mandatory)}
    unsupported: dict[str, str] = {}
    for route in route_list:
        options = _enumerate_paths(route, merged, max_hops=4)
        if not options:
            unsupported[route.question_id] = (
                route.unsupported_reason
                or "No supported path of four or fewer hops was found"
            )
            continue
        option_ids = {
            frozenset(step.relationship_type_id for step in path)
            for path in options
        }
        states = _prune_dominated(
            {state | option for state in states for option in option_ids},
            by_id,
        )

    selected_ids = min(states, key=lambda item: _selection_key(item, by_id))
    if len(selected_ids) > 24:
        raise ProposalSelectionError(
            f"[DOM-103] minimal vocabulary N={len(selected_ids)} exceeds 24"
        )
    selected = tuple(by_id[item_id] for item_id in sorted(selected_ids))
    plans: list[SelectedQuestionPlan] = []
    for route in route_list:
        paths = _enumerate_paths(route, selected, max_hops=4)
        if not paths:
            plans.append(
                SelectedQuestionPlan(
                    question_id=route.question_id,
                    required_path=(),
                    covered=False,
                    unsupported_reason=unsupported.get(
                        route.question_id,
                        "The minimum selected vocabulary does not support this question",
                    ),
                )
            )
        else:
            plans.append(
                SelectedQuestionPlan(
                    question_id=route.question_id,
                    required_path=paths[0],
                    covered=True,
                )
            )
    covered = [plan for plan in plans if plan.covered]
    if not covered:
        raise ProposalSelectionError(
            "[DOM-104] at least one competency question must be covered"
        )
    max_hops = max(plan.hop_count for plan in covered)
    if max_hops == 4:
        for plan in covered:
            if plan.hop_count == 4 and any(
                not step.evidence_span_ids for step in plan.required_path
            ):
                raise ProposalSelectionError(
                    f"[DOM-105] K=4 path {plan.question_id} lacks per-hop evidence"
                )

    rationales: dict[str, tuple[str, ...]] = {}
    if len(selected) > 20:
        for relationship in selected:
            references = tuple(
                sorted(
                    set(relationship.competency_question_ids)
                    | (
                        {relationship.governance_rationale}
                        if relationship.governance_rationale is not None
                        else set()
                    )
                )
            )
            if not references:
                raise ProposalSelectionError(
                    "[DOM-103] N=21..24 requires rationale for every retained type"
                )
            rationales[relationship.relationship_type_id] = references

    # Critical unsupported questions remain visible for approval blocking.
    _ = critical_question_ids & set(unsupported)
    return SelectionResult(
        relationships=selected,
        question_plans=tuple(plans),
        max_hops=max_hops,
        retained_type_rationales=rationales,
        merge_groups=merge_groups,
    )
