"""multitype_plan.py — derive a multi-type ontology plan from canonical Parquet.

The default Fabric ontology models all rows of ``dbo.entities`` as a single
generic ``KGEntity`` type, so the Ontology Explorer shows one box even when the
data contains dozens of real entity types.  This module computes a *richer* plan:
one Fabric EntityType per real domain type (e.g. ``Component``, ``Procedure``),
and one typed RelationshipType per ``(source_type → target_type)`` pair actually
present in the data.

The plan is consumed by :func:`fabric_kg_builder.ontology.fabric_def.build_multitype_ontology_parts`
and by the per-type table materialization in
:func:`fabric_kg_builder.deploy.onelake_multitype.materialize_multitype_tables`.

Design notes
------------
* **Relationship verbs are collapsed by endpoint pair.**  Real data contains
  hundreds of near-synonym verbs (``HAS_STEP``, ``has_step``, ``includes_step``)
  between the same two types.  Modelling each separately yields an unusable
  graph, so we keep one RelationshipType per ``(source_type, target_type)`` pair
  and name it after the dominant (most frequent) verb for that pair.
* **Only types/pairs above a count threshold are modelled**, keeping the graph
  legible.  Thresholds are caller-controlled.
* Pure functions over Arrow tables — no I/O, fully unit-testable.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Default "core" domain types for the Surface support corpus, in display order.
# Callers may override; types not present in the data are dropped automatically.
DEFAULT_CORE_TYPES: list[str] = [
    "Device",
    "DeviceModel",
    "Component",
    "Part",
    "PartNumber",
    "Procedure",
    "Step",
    "Tool",
    "Symptom",
    "Cause",
    "Resolution",
    "Section",
]


def slugify_table(name: str) -> str:
    """Return a lowercase, underscore-safe table-name fragment for *name*."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    return s or "x"


@dataclass
class EntityTypePlan:
    """One Fabric EntityType backed by a per-type Lakehouse table."""

    type_name: str
    table_name: str  # e.g. "entities_component"
    count: int


@dataclass
class RelationshipPairPlan:
    """One typed RelationshipType backed by a per-pair Lakehouse table."""

    name: str  # canonical relationship name (dominant verb), unique in plan
    source_type: str
    target_type: str
    table_name: str  # e.g. "rel_procedure_step"
    count: int


@dataclass
class MultitypePlan:
    """Full multi-type ontology plan."""

    entity_types: list[EntityTypePlan] = field(default_factory=list)
    relationship_pairs: list[RelationshipPairPlan] = field(default_factory=list)

    @property
    def type_names(self) -> list[str]:
        return [e.type_name for e in self.entity_types]


def _read_columns(parquet_path: Path, columns: list[str]) -> dict[str, list[Any]]:
    import pyarrow.parquet as pq  # type: ignore[import]

    table = pq.read_table(str(parquet_path), columns=columns)
    return {c: table.column(c).to_pylist() for c in columns}


def build_plan(
    parquet_dir: Path,
    core_types: list[str] | None = None,
    min_type_count: int = 1,
    min_pair_count: int = 10,
    max_pairs: int = 40,
) -> MultitypePlan:
    """Compute a :class:`MultitypePlan` from canonical Parquet tables.

    Parameters
    ----------
    parquet_dir:
        Directory containing ``entities.parquet`` and ``relationships.parquet``.
    core_types:
        Candidate entity-type names to model (defaults to
        :data:`DEFAULT_CORE_TYPES`).  Types absent from the data, or below
        *min_type_count*, are dropped.
    min_type_count:
        Minimum instance count for a type to be modelled.
    min_pair_count:
        Minimum edge count for a ``(source_type, target_type)`` pair to become a
        typed relationship.
    max_pairs:
        Hard cap on the number of relationship pairs (keeps the graph legible).
    """
    parquet_dir = Path(parquet_dir)
    candidates = list(core_types if core_types is not None else DEFAULT_CORE_TYPES)

    ent = _read_columns(parquet_dir / "entities.parquet", ["entity_id", "entity_type"])
    type_of: dict[str, str] = dict(zip(ent["entity_id"], ent["entity_type"]))
    type_counts = collections.Counter(ent["entity_type"])

    # Keep candidate types that are actually present and above threshold,
    # preserving the caller's display order.
    present_types = [
        t for t in candidates if type_counts.get(t, 0) >= min_type_count
    ]
    present_set = set(present_types)

    entity_plans = [
        EntityTypePlan(
            type_name=t,
            table_name=f"entities_{slugify_table(t)}",
            count=type_counts[t],
        )
        for t in present_types
    ]

    # Relationships: collapse verbs by (source_type, target_type) pair.
    rel = _read_columns(
        parquet_dir / "relationships.parquet",
        ["source_entity_id", "relationship_type", "target_entity_id"],
    )
    pair_counts: collections.Counter[tuple[str, str]] = collections.Counter()
    verb_counts: dict[tuple[str, str], collections.Counter[str]] = (
        collections.defaultdict(collections.Counter)
    )
    for s, verb, t in zip(
        rel["source_entity_id"], rel["relationship_type"], rel["target_entity_id"]
    ):
        st = type_of.get(s)
        tt = type_of.get(t)
        if st in present_set and tt in present_set:
            pair_counts[(st, tt)] += 1
            verb_counts[(st, tt)][(verb or "related_to")] += 1

    # Rank pairs by count, apply threshold + cap.
    ranked = [p for p, n in pair_counts.most_common() if n >= min_pair_count]
    ranked = ranked[:max_pairs]

    used_names: set[str] = set()
    rel_plans: list[RelationshipPairPlan] = []
    for (st, tt) in ranked:
        dominant_verb = verb_counts[(st, tt)].most_common(1)[0][0]
        base = slugify_table(dominant_verb)
        name = base
        # Ensure relationship-type names are unique within the ontology.
        if name in used_names:
            name = f"{base}_{slugify_table(st)}_{slugify_table(tt)}"
        suffix = 2
        while name in used_names:
            name = f"{base}_{slugify_table(st)}_{slugify_table(tt)}_{suffix}"
            suffix += 1
        used_names.add(name)
        rel_plans.append(
            RelationshipPairPlan(
                name=name,
                source_type=st,
                target_type=tt,
                table_name=f"rel_{slugify_table(st)}_{slugify_table(tt)}",
                count=pair_counts[(st, tt)],
            )
        )

    return MultitypePlan(entity_types=entity_plans, relationship_pairs=rel_plans)
