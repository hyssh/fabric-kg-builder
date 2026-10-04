"""GRP-002 (revised): Entity candidate blocking.

Fixes:
- Uses both aliases and search_aliases
- Invalid properties_json raises ValueError (not silently ignored)
- Dedup pair decisions tracked in resolution.py
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from fabric_kg_builder.model.schemas import EntityRow


class InvalidEntityPropertiesError(ValueError):
    """Raised when an entity has malformed properties_json."""


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    lower = ascii_text.lower().strip()
    lower = re.sub(r"[^a-z0-9\s-]", "", lower)
    return re.sub(r"\s+", " ", lower).strip()


def _parse_properties(entity: EntityRow) -> dict:
    if not entity.properties_json:
        return {}
    try:
        result = json.loads(entity.properties_json)
        if not isinstance(result, dict):
            raise InvalidEntityPropertiesError(
                f"Entity {entity.entity_id!r} properties_json must be a JSON object, "
                f"got {type(result).__name__}"
            )
        return result
    except json.JSONDecodeError as exc:
        raise InvalidEntityPropertiesError(
            f"Entity {entity.entity_id!r} has invalid properties_json: {exc}"
        ) from exc


def _blocking_keys(entity: EntityRow) -> frozenset[str]:
    type_key = _normalize(entity.entity_type)
    title_tokens = frozenset(
        t for t in _normalize(entity.display_name).split() if len(t) >= 2
    )
    # Use both aliases and search_aliases
    alias_sources: list[list[str]] = []
    if entity.aliases:
        alias_sources.append(entity.aliases)
    if entity.search_aliases:
        alias_sources.append(entity.search_aliases)
    alias_tokens: set[str] = set()
    for alias_list in alias_sources:
        for alias in alias_list:
            for t in _normalize(alias).split():
                if len(t) >= 2:
                    alias_tokens.add(t)

    props = _parse_properties(entity)
    scope: Optional[str] = props.get("scope")
    identifiers: list[str] = props.get("identifiers", []) if isinstance(props.get("identifiers"), list) else []

    keys: set[str] = set()
    for token in title_tokens | alias_tokens:
        keys.add(f"type:{type_key}|tok:{token}")
    if scope:
        keys.add(f"type:{type_key}|scope:{_normalize(str(scope))}")
    for identifier in identifiers:
        keys.add(f"type:{type_key}|id:{_normalize(str(identifier))}")

    if not keys:
        keys.add(f"type:{type_key}|fallback")

    return frozenset(keys)


@dataclass
class CandidateBlock:
    block_key: str
    entities: list[EntityRow] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entities)


def block_candidates(
    entities: Iterable[EntityRow],
) -> dict[str, CandidateBlock]:
    """Group entities by blocking key. Raises InvalidEntityPropertiesError on bad JSON."""
    blocks: dict[str, CandidateBlock] = {}
    for entity in entities:
        for key in _blocking_keys(entity):
            if key not in blocks:
                blocks[key] = CandidateBlock(block_key=key)
            blocks[key].entities.append(entity)
    return blocks
