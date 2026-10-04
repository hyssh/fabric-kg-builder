"""Tests for graph/blocking.py — entity candidate blocking (GRP-002)."""

from __future__ import annotations

import json

import pytest

from fabric_kg_builder.graph.blocking import (
    CandidateBlock,
    InvalidEntityPropertiesError,
    _blocking_keys,
    _normalize,
    _parse_properties,
    block_candidates,
)
from fabric_kg_builder.model.schemas import EntityRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(
    entity_id: str = "e1",
    entity_type: str = "org",
    display_name: str = "Acme Corp",
    aliases: list[str] | None = None,
    search_aliases: list[str] | None = None,
    properties_json: str | None = None,
) -> EntityRow:
    import hashlib
    from datetime import datetime, timezone
    key = hashlib.sha1(f"{entity_id}:{display_name}".encode()).hexdigest()[:16]
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
# _normalize
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_lowercase(self) -> None:
        assert _normalize("HELLO") == "hello"

    def test_strips_accents(self) -> None:
        assert _normalize("Ünited") == "united"

    def test_removes_special_chars(self) -> None:
        result = _normalize("foo@bar!")
        assert "@" not in result
        assert "!" not in result

    def test_collapses_whitespace(self) -> None:
        assert _normalize("  foo   bar  ") == "foo bar"

    def test_hyphen_preserved(self) -> None:
        assert "-" in _normalize("supply-chain")

    def test_empty_string(self) -> None:
        assert _normalize("") == ""


# ---------------------------------------------------------------------------
# _parse_properties
# ---------------------------------------------------------------------------


class TestParseProperties:
    def test_none_returns_empty(self) -> None:
        entity = _entity(properties_json=None)
        assert _parse_properties(entity) == {}

    def test_valid_json_object(self) -> None:
        entity = _entity(properties_json='{"scope": "global"}')
        result = _parse_properties(entity)
        assert result == {"scope": "global"}

    def test_invalid_json_raises(self) -> None:
        entity = _entity(properties_json="not-json")
        with pytest.raises(InvalidEntityPropertiesError):
            _parse_properties(entity)

    def test_json_array_raises(self) -> None:
        entity = _entity(properties_json='["a", "b"]')
        with pytest.raises(InvalidEntityPropertiesError, match="JSON object"):
            _parse_properties(entity)


# ---------------------------------------------------------------------------
# _blocking_keys
# ---------------------------------------------------------------------------


class TestBlockingKeys:
    def test_returns_frozenset(self) -> None:
        entity = _entity()
        keys = _blocking_keys(entity)
        assert isinstance(keys, frozenset)

    def test_contains_type_and_token(self) -> None:
        entity = _entity(entity_type="org", display_name="Acme Corp")
        keys = _blocking_keys(entity)
        assert any("type:org" in k for k in keys)

    def test_uses_aliases(self) -> None:
        entity = _entity(aliases=["AcmeAlias"])
        keys = _blocking_keys(entity)
        assert any("acmealias" in k for k in keys)

    def test_uses_search_aliases(self) -> None:
        entity = _entity(search_aliases=["SearchTerm"])
        keys = _blocking_keys(entity)
        assert any("searchterm" in k for k in keys)

    def test_scope_in_properties(self) -> None:
        entity = _entity(properties_json='{"scope": "global"}')
        keys = _blocking_keys(entity)
        assert any("scope:global" in k for k in keys)

    def test_identifiers_in_properties(self) -> None:
        entity = _entity(properties_json='{"identifiers": ["ID-001"]}')
        keys = _blocking_keys(entity)
        assert any("id:id-001" in k for k in keys)

    def test_fallback_key_when_no_tokens(self) -> None:
        # Very short display_name (< 2 chars) gets no token keys → uses fallback
        entity = _entity(display_name="A", entity_type="x")
        keys = _blocking_keys(entity)
        assert any("fallback" in k for k in keys)

    def test_invalid_identifiers_not_list_ignored(self) -> None:
        entity = _entity(properties_json='{"identifiers": "not-a-list"}')
        # Should not raise, but identifiers key shouldn't be present
        keys = _blocking_keys(entity)
        assert isinstance(keys, frozenset)


# ---------------------------------------------------------------------------
# CandidateBlock
# ---------------------------------------------------------------------------


class TestCandidateBlock:
    def test_len_matches_entities(self) -> None:
        block = CandidateBlock(block_key="k", entities=[_entity(), _entity("e2")])
        assert len(block) == 2

    def test_empty_block(self) -> None:
        block = CandidateBlock(block_key="k")
        assert len(block) == 0


# ---------------------------------------------------------------------------
# block_candidates
# ---------------------------------------------------------------------------


class TestBlockCandidates:
    def test_empty_input(self) -> None:
        result = block_candidates([])
        assert result == {}

    def test_single_entity_creates_blocks(self) -> None:
        entity = _entity()
        result = block_candidates([entity])
        assert len(result) > 0
        # Entity should be in at least one block
        all_entities = {e.entity_id for block in result.values() for e in block.entities}
        assert "e1" in all_entities

    def test_shared_token_same_block(self) -> None:
        e1 = _entity("e1", display_name="Alpha Beta")
        e2 = _entity("e2", display_name="Alpha Gamma")
        result = block_candidates([e1, e2])
        # "alpha" token should appear in a block key shared by both
        shared_blocks = [b for b in result.values() if len(b.entities) > 1]
        assert len(shared_blocks) > 0

    def test_different_types_separate_blocks(self) -> None:
        e1 = _entity("e1", entity_type="org", display_name="Alpha")
        e2 = _entity("e2", entity_type="location", display_name="Alpha")
        result = block_candidates([e1, e2])
        # Different types mean different block keys for same token
        # e1 and e2 won't share any block
        shared_blocks = [b for b in result.values() if len(b.entities) > 1]
        assert len(shared_blocks) == 0

    def test_invalid_properties_raises(self) -> None:
        entity = _entity(properties_json="bad-json")
        with pytest.raises(InvalidEntityPropertiesError):
            block_candidates([entity])

    def test_block_keys_are_strings(self) -> None:
        entity = _entity()
        result = block_candidates([entity])
        for key in result:
            assert isinstance(key, str)
