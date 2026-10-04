"""GRP-003 (revised): Entity resolution with dedup pair decisions.

Fix: deduplicates pair decisions so entities appearing in multiple blocks
produce only one decision per pair, retaining REVIEW decisions when
either block produced REVIEW.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from fabric_kg_builder.model.schemas import EntityRow

ScopeCompatibilityMap = dict[str, set[str]]
_DEFAULT_SCOPE_MAP: ScopeCompatibilityMap = {}


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s-]", "", ascii_text.lower().strip())).strip()


def _extract_scope(entity: EntityRow) -> Optional[str]:
    if entity.properties_json:
        try:
            return json.loads(entity.properties_json).get("scope")
        except (ValueError, AttributeError):
            pass
    return None


def _scopes_compatible(
    scope_a: Optional[str],
    scope_b: Optional[str],
    compat_map: ScopeCompatibilityMap,
) -> bool:
    if scope_a is None and scope_b is None:
        return True
    if scope_a is None or scope_b is None:
        return False
    if scope_a == scope_b:
        return True
    return scope_b in compat_map.get(scope_a, set())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union)


def _alias_set(entity: EntityRow) -> set[str]:
    aliases: list[str] = []
    if entity.aliases:
        aliases.extend(entity.aliases)
    if entity.search_aliases:
        aliases.extend(entity.search_aliases)
    return {_normalize(a) for a in aliases}


class ResolutionDecision(str, Enum):
    SAME = "same"
    DIFFERENT = "different"
    REVIEW = "review"


@dataclass
class ResolutionResult:
    entity_a_id: str
    entity_b_id: str
    decision: ResolutionDecision
    confidence: float
    reason: str
    merged_entity_id: Optional[str] = field(default=None)

    def is_merge(self) -> bool:
        return self.decision == ResolutionDecision.SAME

    def canonical_pair_key(self) -> tuple[str, str]:
        return (min(self.entity_a_id, self.entity_b_id),
                max(self.entity_a_id, self.entity_b_id))


def _decision_priority(d: ResolutionDecision) -> int:
    return {
        ResolutionDecision.REVIEW: 0,
        ResolutionDecision.SAME: 1,
        ResolutionDecision.DIFFERENT: 2,
    }[d]


def _resolve_pair(
    a: EntityRow,
    b: EntityRow,
    compat_map: ScopeCompatibilityMap,
) -> ResolutionResult:
    if _normalize(a.entity_type) != _normalize(b.entity_type):
        return ResolutionResult(
            entity_a_id=a.entity_id, entity_b_id=b.entity_id,
            decision=ResolutionDecision.DIFFERENT, confidence=0.99,
            reason=f"entity_type mismatch: {a.entity_type!r} vs {b.entity_type!r}",
        )
    if (
        a.resolution_context_key
        and b.resolution_context_key
        and a.resolution_context_key != b.resolution_context_key
    ):
        return ResolutionResult(
            entity_a_id=a.entity_id,
            entity_b_id=b.entity_id,
            decision=ResolutionDecision.DIFFERENT,
            confidence=0.99,
            reason="Explicit parent/location/identifier context differs",
        )
    cannot_a = set(a.cannot_link_keys or [])
    cannot_b = set(b.cannot_link_keys or [])
    if cannot_a and cannot_b and cannot_a != cannot_b:
        return ResolutionResult(
            entity_a_id=a.entity_id,
            entity_b_id=b.entity_id,
            decision=ResolutionDecision.DIFFERENT,
            confidence=0.99,
            reason="Explicit cannot-link discriminator differs",
        )
    scope_a = _extract_scope(a)
    scope_b = _extract_scope(b)
    compatible = _scopes_compatible(scope_a, scope_b, compat_map)
    norm_a = _normalize(a.display_name)
    norm_b = _normalize(b.display_name)
    exact = norm_a == norm_b

    if exact and not compatible:
        return ResolutionResult(
            entity_a_id=a.entity_id, entity_b_id=b.entity_id,
            decision=ResolutionDecision.REVIEW, confidence=0.5,
            reason=f"Exact title match but incompatible scopes: {scope_a!r} vs {scope_b!r}",
        )
    if exact and compatible:
        return ResolutionResult(
            entity_a_id=a.entity_id, entity_b_id=b.entity_id,
            decision=ResolutionDecision.SAME, confidence=0.95,
            reason="Exact normalized title match with compatible scope",
            merged_entity_id=a.entity_id,
        )
    if a.canonical_key and b.canonical_key and a.canonical_key == b.canonical_key:
        if not compatible:
            return ResolutionResult(
                entity_a_id=a.entity_id, entity_b_id=b.entity_id,
                decision=ResolutionDecision.REVIEW, confidence=0.5,
                reason="Canonical key match but incompatible scopes",
            )
        return ResolutionResult(
            entity_a_id=a.entity_id, entity_b_id=b.entity_id,
            decision=ResolutionDecision.SAME, confidence=0.92,
            reason="Canonical key match with compatible scope",
            merged_entity_id=a.entity_id,
        )
    aliases_a = _alias_set(a) | {norm_a}
    aliases_b = _alias_set(b) | {norm_b}
    if aliases_a & aliases_b:
        if not compatible:
            return ResolutionResult(
                entity_a_id=a.entity_id, entity_b_id=b.entity_id,
                decision=ResolutionDecision.REVIEW, confidence=0.45,
                reason="Alias overlap but incompatible scopes",
            )
        return ResolutionResult(
            entity_a_id=a.entity_id, entity_b_id=b.entity_id,
            decision=ResolutionDecision.SAME, confidence=0.80,
            reason="Alias set intersection with compatible scope",
            merged_entity_id=a.entity_id,
        )
    tokens_a = {t for t in norm_a.split() if len(t) >= 2}
    tokens_b = {t for t in norm_b.split() if len(t) >= 2}
    jaccard = _jaccard(tokens_a, tokens_b)
    if jaccard >= 0.6:
        return ResolutionResult(
            entity_a_id=a.entity_id, entity_b_id=b.entity_id,
            decision=ResolutionDecision.REVIEW, confidence=jaccard * 0.7,
            reason=f"High Jaccard similarity ({jaccard:.2f}) — needs review",
        )
    return ResolutionResult(
        entity_a_id=a.entity_id, entity_b_id=b.entity_id,
        decision=ResolutionDecision.DIFFERENT, confidence=1.0 - jaccard,
        reason=f"Low similarity (Jaccard={jaccard:.2f}), no alias match",
    )


def resolve_candidates(
    candidates: list[EntityRow],
    *,
    scope_compatibility: Optional[ScopeCompatibilityMap] = None,
) -> list[ResolutionResult]:
    """Resolve all pairs — one result per unique pair (deduped by canonical pair key)."""
    compat = scope_compatibility or _DEFAULT_SCOPE_MAP
    seen: dict[tuple[str, str], ResolutionResult] = {}
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            result = _resolve_pair(a, b, compat)
            key = result.canonical_pair_key()
            if key not in seen:
                seen[key] = result
            else:
                existing = seen[key]
                # Keep the result with the highest priority decision (REVIEW > SAME > DIFFERENT)
                if _decision_priority(result.decision) < _decision_priority(existing.decision):
                    seen[key] = result
    return list(seen.values())
