"""Canonical/semantic Parquet source resolution shared by compile and deploy."""

from __future__ import annotations

from pathlib import Path

SOURCE_TABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "entities": ("entities", "semantic_entities"),
    "semantic_entities": ("semantic_entities", "entities"),
    "relationships": ("relationships", "semantic_relationships"),
    "semantic_relationships": ("semantic_relationships", "relationships"),
}


def source_table_candidates(source_table_name: str) -> tuple[str, ...]:
    """Return exact-first compatible source names for one semantic table."""
    table_name = source_table_name.removesuffix(".parquet")
    if not table_name or Path(table_name).name != table_name:
        raise ValueError(f"Unsafe source table name: {source_table_name!r}.")
    return SOURCE_TABLE_ALIASES.get(table_name, (table_name,))


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
