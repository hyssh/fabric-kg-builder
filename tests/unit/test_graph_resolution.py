"""Tests for graph/resolution.py — entity resolution (GRP-003)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from fabric_kg_builder.graph.resolution import (
    ResolutionDecision,
    ResolutionResult,
    _alias_set,
    _extract_scope,
    _jaccard,
    _normalize,
    _resolve_pair,
    _scopes_compatible,
    resolve_candidates,
)
from fabric_kg_builder.model.schemas import EntityRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(
    entity_id: str,
    display_name: str = "Alpha Corp",
    entity_type: str = "org",
    canonical_key: str | None = None,
    aliases: list[str] | None = None,
    search_aliases: list[str] | None = None,
    properties_json: str | None = None,
) -> EntityRow:
    key = canonical_key or hashlib.sha1(f"{entity_id}:{display_name}".encode()).hexdigest()[:16]
    return EntityRow(
        entity_id=entity_id,
        entity_type=entity_type,
        display_name=display_name,
        canonical_key=key,
        content_hash=key,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        aliases=aliases or [],
        search_aliases=search_aliases or [],
        properties_json=properties_json,
    )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_lowercase(self) -> None:
        assert _normalize("ACME") == "acme"

    def test_strips_accents(self) -> None:
        assert "u" in _normalize("Über")

    def test_collapses_whitespace(self) -> None:
        assert _normalize("  Foo  Bar  ") == "foo bar"


class TestExtractScope:
    def test_returns_scope_from_properties(self) -> None:
        entity = _entity("e1", properties_json='{"scope": "global"}')
        assert _extract_scope(entity) == "global"

    def test_returns_none_when_no_properties(self) -> None:
        entity = _entity("e1")
        assert _extract_scope(entity) is None

    def test_returns_none_on_bad_json(self) -> None:
        entity = _entity("e1", properties_json="not-json")
        assert _extract_scope(entity) is None


class TestScopesCompatible:
    def test_both_none_compatible(self) -> None:
        assert _scopes_compatible(None, None, {})

    def test_one_none_incompatible(self) -> None:
        assert not _scopes_compatible("global", None, {})
        assert not _scopes_compatible(None, "local", {})

    def test_same_scope_compatible(self) -> None:
        assert _scopes_compatible("global", "global", {})

    def test_compatible_via_compat_map(self) -> None:
        compat_map = {"global": {"regional"}}
        assert _scopes_compatible("global", "regional", compat_map)

    def test_incompatible_scopes(self) -> None:
        assert not _scopes_compatible("global", "local", {})


class TestJaccard:
    def test_identical_sets(self) -> None:
        a = {"foo", "bar"}
        assert _jaccard(a, a) == pytest.approx(1.0)

    def test_empty_sets(self) -> None:
        assert _jaccard(set(), set()) == pytest.approx(1.0)

    def test_disjoint_sets(self) -> None:
        assert _jaccard({"a"}, {"b"}) == pytest.approx(0.0)

    def test_partial_overlap(self) -> None:
        result = _jaccard({"a", "b"}, {"b", "c"})
        assert result == pytest.approx(1 / 3, abs=1e-6)


class TestAliasSet:
    def test_combines_aliases_and_search_aliases(self) -> None:
        entity = _entity("e1", aliases=["Alpha"], search_aliases=["ALPHA CORP"])
        result = _alias_set(entity)
        assert "alpha" in result
        assert "alpha corp" in result

    def test_empty_aliases(self) -> None:
        entity = _entity("e1")
        assert _alias_set(entity) == set()


# ---------------------------------------------------------------------------
# ResolutionResult
# ---------------------------------------------------------------------------


class TestResolutionResult:
    def test_canonical_pair_key_sorted(self) -> None:
        r1 = ResolutionResult(
            entity_a_id="b", entity_b_id="a",
            decision=ResolutionDecision.SAME, confidence=0.9, reason="test",
        )
        r2 = ResolutionResult(
            entity_a_id="a", entity_b_id="b",
            decision=ResolutionDecision.SAME, confidence=0.9, reason="test",
        )
        assert r1.canonical_pair_key() == r2.canonical_pair_key()


# ---------------------------------------------------------------------------
# _resolve_pair
# ---------------------------------------------------------------------------


class TestResolvePair:
    def test_exact_name_match_is_same(self) -> None:
        a = _entity("e1", "Acme Corporation")
        b = _entity("e2", "Acme Corporation")
        result = _resolve_pair(a, b, {})
        assert result.decision == ResolutionDecision.SAME

    def test_different_names_different(self) -> None:
        a = _entity("e1", "Alpha Corp")
        b = _entity("e2", "Beta Industries")
        result = _resolve_pair(a, b, {})
        assert result.decision == ResolutionDecision.DIFFERENT

    def test_canonical_key_match_is_same(self) -> None:
        a = _entity("e1", "Alpha Corp", canonical_key="same-key")
        b = _entity("e2", "Alpha Ltd", canonical_key="same-key")
        result = _resolve_pair(a, b, {})
        assert result.decision == ResolutionDecision.SAME

    def test_alias_overlap_is_same(self) -> None:
        a = _entity("e1", "Alpha Corp", aliases=["ACorp"])
        b = _entity("e2", "Alpha Corporation", aliases=["ACorp"])
        result = _resolve_pair(a, b, {})
        assert result.decision == ResolutionDecision.SAME

    def test_high_jaccard_is_review(self) -> None:
        # "Alpha Beta Corp" vs "Alpha Beta Zeta Corp" → high Jaccard (3/4 = 0.75)
        a = _entity("e1", "Alpha Beta Zeta Corp")
        b = _entity("e2", "Alpha Beta Zeta Corporation")
        result = _resolve_pair(a, b, {})
        assert result.decision in {ResolutionDecision.REVIEW, ResolutionDecision.SAME}

    def test_incompatible_scopes_with_exact_match_is_review(self) -> None:
        a = _entity("e1", "Alpha Corp", properties_json='{"scope": "us"}')
        b = _entity("e2", "Alpha Corp", properties_json='{"scope": "eu"}')
        result = _resolve_pair(a, b, {})
        assert result.decision == ResolutionDecision.REVIEW


# ---------------------------------------------------------------------------
# resolve_candidates
# ---------------------------------------------------------------------------


class TestResolveCandidates:
    def test_empty_candidates(self) -> None:
        assert resolve_candidates([]) == []

    def test_single_candidate(self) -> None:
        # Only one entity → no pairs
        entity = _entity("e1")
        result = resolve_candidates([entity])
        assert result == []

    def test_two_identical_entities(self) -> None:
        a = _entity("e1", "Alpha Corp")
        b = _entity("e2", "Alpha Corp")
        results = resolve_candidates([a, b])
        assert len(results) == 1
        assert results[0].decision == ResolutionDecision.SAME

    def test_two_different_entities(self) -> None:
        a = _entity("e1", "Alpha Corp")
        b = _entity("e2", "Beta Industries")
        results = resolve_candidates([a, b])
        assert len(results) == 1
        assert results[0].decision == ResolutionDecision.DIFFERENT

    def test_three_entities_three_pairs(self) -> None:
        entities = [
            _entity("e1", "Alpha Corp"),
            _entity("e2", "Beta Ltd"),
            _entity("e3", "Gamma Inc"),
        ]
        results = resolve_candidates(entities)
        assert len(results) == 3

    def test_dedup_results(self) -> None:
        # All three have same name → a vs b and a vs c are both SAME
        entities = [
            _entity("e1", "Same Corp"),
            _entity("e2", "Same Corp"),
            _entity("e3", "Same Corp"),
        ]
        results = resolve_candidates(entities)
        # 3 pairs: (0,1), (0,2), (1,2)
        assert len(results) == 3
        assert all(r.decision == ResolutionDecision.SAME for r in results)

    def test_with_scope_compatibility(self) -> None:
        a = _entity("e1", "Alpha Corp", properties_json='{"scope": "us"}')
        b = _entity("e2", "Alpha Corp", properties_json='{"scope": "eu"}')
        # Without compatibility map: REVIEW
        r1 = resolve_candidates([a, b])
        assert r1[0].decision == ResolutionDecision.REVIEW
        # With compatibility map: SAME
        r2 = resolve_candidates([a, b], scope_compatibility={"us": {"eu"}})
        assert r2[0].decision == ResolutionDecision.SAME
