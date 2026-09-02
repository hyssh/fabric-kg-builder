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
* **All observed domain types are candidates by default.**  Named profiles can
  explicitly restrict the model for sample or curated domains.
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

from fabric_kg_builder.semantic.source_tables import (
    resolve_semantic_source_parquet,
)

#: Default location of the per-project type-profile registry, relative to the
#: project root.  The file is *optional*: when it is absent, every observed
#: entity type is a candidate and no allowlist is applied.
DEFAULT_TYPE_PROFILES_PATH = Path("ontology") / "type-profiles.yaml"


def load_type_profiles(path: str | Path | None = None) -> dict[str, list[str]]:
    """Load the named entity-type allowlists declared for this project.

    The registry is project data, not library data: profiles describe one
    domain's vocabulary and therefore must never be compiled into the tool.
    Returns an empty mapping when no registry file exists.

    Expected shape::

        profiles:
          <profile-name>: [TypeA, TypeB, ...]
    """
    import yaml  # noqa: PLC0415 — optional dependency at call time only

    resolved = Path(path) if path is not None else DEFAULT_TYPE_PROFILES_PATH
    if not resolved.is_file():
        return {}
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{resolved}: type-profile registry must be a YAML mapping, "
            f"got {type(raw).__name__}"
        )
    profiles = raw.get("profiles", raw)
    if not isinstance(profiles, dict):
        raise ValueError(f"{resolved}: 'profiles' must be a mapping of name -> type list")
    out: dict[str, list[str]] = {}
    for name, types in profiles.items():
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
            raise ValueError(
                f"{resolved}: profile {name!r} must be a list of type-name strings"
            )
        out[str(name)] = list(types)
    return out


def get_type_profile(
    name_or_path: str, *, profiles_path: str | Path | None = None
) -> list[str]:
    """Resolve an explicit entity-type allowlist.

    *name_or_path* is one of:

    * a path to a YAML file holding a bare list of type names, or a mapping
      with an ``entity_types`` list;
    * a path to a profile registry (a mapping with ``profiles``) that declares
      exactly one profile — the unambiguous case;
    * the name of a profile declared in the project's registry
      (``ontology/type-profiles.yaml`` by default).

    No profile is built in.  A tool that ships one domain's type vocabulary
    silently privileges that domain, so the vocabulary lives in project data
    and this function only resolves it.
    """
    import yaml  # noqa: PLC0415

    candidate = Path(name_or_path)
    if candidate.is_file():
        raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or []
        if isinstance(raw, dict) and "profiles" in raw:
            declared = load_type_profiles(candidate)
            if len(declared) == 1:
                return next(iter(declared.values()))
            names = ", ".join(sorted(declared)) or "(none)"
            raise ValueError(
                f"{candidate} declares {len(declared)} profiles ({names}); a path "
                "alone is ambiguous. Place the registry at "
                f"{DEFAULT_TYPE_PROFILES_PATH} and pass the profile name instead."
            )
        types = raw.get("entity_types", raw) if isinstance(raw, dict) else raw
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
            raise ValueError(
                f"{candidate}: expected a YAML list of type names, a mapping with an "
                "'entity_types' list, or a registry with a single 'profiles' entry"
            )
        return list(types)

    profiles = load_type_profiles(profiles_path)
    try:
        return list(profiles[name_or_path])
    except KeyError as exc:
        registry = Path(profiles_path) if profiles_path else DEFAULT_TYPE_PROFILES_PATH
        if not profiles:
            raise ValueError(
                f"Unknown ontology type profile {name_or_path!r}: no profile registry "
                f"found at {registry} and no such file on disk. Declare profiles there, "
                "pass a path to a type-list YAML, or omit --type-profile to derive "
                "types from the observed data."
            ) from exc
        available = ", ".join(sorted(profiles))
        raise ValueError(
            f"Unknown ontology type profile {name_or_path!r}. "
            f"Declared in {registry}: {available}"
        ) from exc


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
    source_types: tuple[str, ...] = ()


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
        Directory containing ``semantic_entities.parquet`` and
        ``semantic_relationships.parquet``.  Falls back to the canonical source
        files for backwards-compatible packages.
    core_types:
        Optional candidate entity-type names to model. When omitted, candidates
        are derived from the observed ``entity_type`` values in first-seen
        order. Types absent from the data, or below *min_type_count*, are
        dropped.
    min_type_count:
        Minimum instance count for a type to be modelled.
    min_pair_count:
        Minimum edge count for a ``(source_type, target_type)`` pair to become a
        typed relationship.
    max_pairs:
        Hard cap on the number of relationship pairs (keeps the graph legible).
    """
    parquet_dir = Path(parquet_dir)
    ent_path = resolve_semantic_source_parquet(
        parquet_dir,
        "semantic_entities",
    )
    rel_path = resolve_semantic_source_parquet(
        parquet_dir,
        "semantic_relationships",
    )
    ent = _read_columns(ent_path, ["entity_id", "entity_type"])
    type_of: dict[str, str] = dict(zip(ent["entity_id"], ent["entity_type"]))
    type_counts = collections.Counter(ent["entity_type"])
    if core_types is None:
        candidates = list(
            dict.fromkeys(
                type_name
                for type_name in ent["entity_type"]
                if isinstance(type_name, str) and type_name.strip()
            )
        )
    else:
        candidates = list(dict.fromkeys(core_types))

    # Fold spelling variants such as "equipment asset" and "equipment_asset"
    # into one ontology type/table so their data cannot overwrite one another.
    groups: dict[str, list[str]] = {}
    for candidate in candidates:
        if type_counts.get(candidate, 0) < min_type_count:
            continue
        groups.setdefault(slugify_table(candidate), []).append(candidate)

    entity_plans = [
        EntityTypePlan(
            type_name=variants[0],
            table_name=f"entities_{slug}",
            count=sum(type_counts[variant] for variant in variants),
            source_types=tuple(variants),
        )
        for slug, variants in groups.items()
    ]
    normalized_type_of = {
        raw_type: plan.type_name
        for plan in entity_plans
        for raw_type in plan.source_types
    }
    present_set = {plan.type_name for plan in entity_plans}

    # Relationships: collapse verbs by (source_type, target_type) pair.
    rel = _read_columns(
        rel_path,
        ["source_entity_id", "relationship_type", "target_entity_id"],
    )
    pair_counts: collections.Counter[tuple[str, str]] = collections.Counter()
    verb_counts: dict[tuple[str, str], collections.Counter[str]] = (
        collections.defaultdict(collections.Counter)
    )
    for s, verb, t in zip(
        rel["source_entity_id"], rel["relationship_type"], rel["target_entity_id"]
    ):
        st = normalized_type_of.get(type_of.get(s, ""))
        tt = normalized_type_of.get(type_of.get(t, ""))
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
