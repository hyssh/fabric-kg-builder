"""Versioned semantic/support source taxonomy and Parquet resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SOURCE_TAXONOMY_VERSION = "1.0"
SourceCategory = Literal[
    "semantic_entity_projection",
    "semantic_relationship_projection",
    "canonical_support_entity",
    "validation_support",
    "denied_candidate_audit",
]

@dataclass(frozen=True)
class SourceRule:
    """Closed schema-2 publication rule for one canonical table."""

    category: SourceCategory
    primary_key: str


SOURCE_TAXONOMY: dict[str, SourceRule] = {
    "semantic_entities": SourceRule(
        "semantic_entity_projection", "entity_id"
    ),
    "semantic_relationships": SourceRule(
        "semantic_relationship_projection", "relationship_id"
    ),
    "source_files": SourceRule("canonical_support_entity", "source_file_id"),
    "chunks": SourceRule("canonical_support_entity", "chunk_id"),
    "document_elements": SourceRule(
        "canonical_support_entity", "document_element_id"
    ),
    "visual_assets": SourceRule("canonical_support_entity", "image_id"),
    "visual_regions": SourceRule(
        "canonical_support_entity", "visual_region_id"
    ),
    "evidence": SourceRule("validation_support", "evidence_id"),
    "entities": SourceRule("denied_candidate_audit", "entity_id"),
    "relationships": SourceRule(
        "denied_candidate_audit", "relationship_id"
    ),
}

SOURCE_TABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "entities": ("entities", "semantic_entities"),
    "semantic_entities": ("semantic_entities", "entities"),
    "relationships": ("relationships", "semantic_relationships"),
    "semantic_relationships": ("semantic_relationships", "relationships"),
}


def source_table_candidates(source_table_name: str) -> tuple[str, ...]:
    """Return exact-first source names for the schema-1 compatibility path."""
    table_name = source_table_name.removesuffix(".parquet")
    if not table_name or Path(table_name).name != table_name:
        raise ValueError(f"Unsafe source table name: {source_table_name!r}.")
    return SOURCE_TABLE_ALIASES.get(table_name, (table_name,))


def source_category(source_table_name: str) -> SourceCategory:
    """Return the closed schema-2 category for an exact source table."""
    table_name = source_table_name.removesuffix(".parquet")
    if not table_name or Path(table_name).name != table_name:
        raise ValueError(f"Unsafe source table name: {source_table_name!r}.")
    try:
        return SOURCE_TAXONOMY[table_name].category
    except KeyError as exc:
        raise ValueError(
            f"Schema-2 source table '{table_name}' is not registered in "
            f"taxonomy {SOURCE_TAXONOMY_VERSION}."
        ) from exc


def source_primary_key(source_table_name: str) -> str:
    """Return the stable primary key for one registered source."""
    table_name = source_table_name.removesuffix(".parquet")
    source_category(table_name)
    return SOURCE_TAXONOMY[table_name].primary_key


def resolve_schema2_source_parquet(
    parquet_dir: Path | str,
    source_table_name: str,
    *,
    allowed_categories: frozenset[SourceCategory] | None = None,
) -> Path:
    """Resolve one exact allowlisted schema-2 source without aliases."""
    table_name = source_table_name.removesuffix(".parquet")
    category = source_category(table_name)
    if category == "denied_candidate_audit":
        raise ValueError(
            f"Schema-2 typed publication cannot use raw candidate table "
            f"'{table_name}'."
        )
    if allowed_categories is not None and category not in allowed_categories:
        raise ValueError(
            f"Schema-2 source table '{table_name}' has category '{category}', "
            f"expected one of {sorted(allowed_categories)}."
        )
    path = Path(parquet_dir) / f"{table_name}.parquet"
    if not path.is_file():
        raise FileNotFoundError(
            f"Required schema-2 source Parquet '{path}' does not exist."
        )
    return path


def resolve_semantic_source_parquet(
    parquet_dir: Path | str,
    source_table_name: str,
) -> Path:
    """Resolve exact mapping name, then its canonical/semantic counterpart."""
    root = Path(parquet_dir)
    candidates = source_table_candidates(source_table_name)
    for candidate in candidates:
        path = root / f"{candidate}.parquet"
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"No source Parquet found for '{source_table_name}'. Tried "
        f"{[f'{candidate}.parquet' for candidate in candidates]} "
        f"under '{root}'."
    )
