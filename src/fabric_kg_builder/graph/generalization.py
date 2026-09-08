"""Corpus-scale generalization stage — fit the *inventory* in one context.

Extraction runs per work unit, and a work unit only ever sees its own slice of
text.  That is unavoidable: the document corpus does not fit in a model
context.  The consequence is that nothing in the pipeline ever observes how the
corpus as a whole names and types things, so surface-form drift between
documents becomes permanent node fragmentation and per-call type disagreement.

The *inventory* of already-extracted labels, however, is much smaller than the
corpus that produced it — it is deduplicated, carries no source text, and grows
with the number of distinct things rather than the number of characters.  It
frequently does fit in a single context.  That makes it the one place in the
pipeline where a generalization decision can be made with global visibility.

This module is the deterministic substrate for that stage.  It does not call a
model.  It builds the compact inventory, measures it against a caller-supplied
budget, and — when the budget is exceeded — partitions it in a way that keeps
co-referring variants together, because a partition that separates ``Widget
Z100`` from ``widget z-100`` cannot merge them no matter how good the model is.

Two properties are deliberate:

* **No domain vocabulary.**  Nothing here knows what domain it is processing.
  Every grouping decision derives from the labels supplied at runtime.
* **Script-neutral.**  Normalization preserves letters and digits from every
  script.  See :func:`normalize_label`; the older
  :mod:`fabric_kg_builder.graph.blocking` normalizer is Latin-only and is not
  reused here.

Usage::

    items = entity_rows_to_items(rows)
    inventory = build_inventory(items)
    plan = plan_generalization(inventory, budget_chars=20_000)
    if plan.fits_single_context:
        prompt_block = plan.batches[0].render()
    else:
        for batch in plan.batches:
            ...  # one call per batch, carrying prior decisions forward
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Sequence

__all__ = [
    "GeneralizationPlan",
    "Inventory",
    "InventoryBatch",
    "InventoryEntry",
    "InventoryItem",
    "OversizedComponent",
    "build_inventory",
    "co_reference_components",
    "entity_rows_to_items",
    "estimate_cost",
    "label_tokens",
    "normalize_label",
    "plan_generalization",
    "relationship_rows_to_items",
]

ENTITY_KIND = "entity"
RELATION_KIND = "relation"
_KINDS = (ENTITY_KIND, RELATION_KIND)

#: Minimum length for a token to be usable as a grouping signal.  Shorter
#: tokens are kept only when the whole label is that short.
_MIN_TOKEN_LEN = 2


def _is_ideographic(char: str) -> bool:
    """True for scripts that are not whitespace-delimited (CJK, Hangul syllables)."""
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF  # Hiragana + Katakana
        or 0x3400 <= code <= 0x4DBF  # CJK Ext A
        or 0x4E00 <= code <= 0x9FFF  # CJK Unified
        or 0xAC00 <= code <= 0xD7A3  # Hangul syllables
        or 0xF900 <= code <= 0xFAFF  # CJK compatibility
    )


def normalize_label(text: str) -> str:
    """Casefold and strip punctuation while preserving every script.

    Unlike :func:`fabric_kg_builder.graph.blocking._normalize`, this does not
    restrict output to ASCII.  That normalizer maps ``"한국어 부품"`` and
    ``"株式会社テスト"`` to the empty string, and maps ``"Виджет 7"`` to
    ``"7"`` — which both loses non-Latin entities entirely and makes unrelated
    non-Latin entities collide on their digits alone.

    Punctuation and symbols become spaces rather than being deleted, so
    ``"widget-z100"`` and ``"widget z100"`` agree while ``"az"`` and ``"a z"``
    stay distinct.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    out: list[str] = []
    for char in folded:
        category = unicodedata.category(char)
        if category[0] in ("L", "N", "M"):
            out.append(char)
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def label_tokens(text: str) -> frozenset[str]:
    """Grouping tokens for ``text``, robust across writing systems.

    Whitespace-delimited scripts contribute their words.  Ideographic scripts
    are not whitespace-delimited, so a run of such characters additionally
    contributes its character bigrams; without this a CJK label would yield a
    single opaque token and could never be grouped with a variant of itself.
    """
    normalized = normalize_label(text)
    if not normalized:
        return frozenset()
    tokens: set[str] = set()
    for word in normalized.split():
        if len(word) >= _MIN_TOKEN_LEN:
            tokens.add(word)
        for run in _ideographic_runs(word):
            if len(run) == 1:
                tokens.add(run)
                continue
            for index in range(len(run) - 1):
                tokens.add(run[index : index + 2])
    if not tokens:
        # Whole label is shorter than the token floor; keep it verbatim so the
        # entry still groups with identical labels instead of vanishing.
        tokens.add(normalized)
    return frozenset(tokens)


def _ideographic_runs(word: str) -> list[str]:
    runs: list[str] = []
    current: list[str] = []
    for char in word:
        if _is_ideographic(char):
            current.append(char)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


@dataclass(frozen=True)
class InventoryItem:
    """One extracted node or edge, reduced to what generalization needs."""

    kind: str
    node_id: str
    label: str
    type_label: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}, got {self.kind!r}")
        if not self.node_id:
            raise ValueError("node_id must be non-empty")


@dataclass(frozen=True)
class InventoryEntry:
    """Distinct (kind, type, normalized label) with the nodes that produced it."""

    kind: str
    type_label: str
    label: str
    normalized_label: str
    node_ids: tuple[str, ...]
    variants: tuple[str, ...]

    @property
    def occurrence_count(self) -> int:
        return len(self.node_ids)

    def render(self) -> str:
        """One compact line.  Variants are included — they are the merge signal."""
        variant_suffix = ""
        extra = tuple(v for v in self.variants if v != self.label)
        if extra:
            variant_suffix = " | " + " ; ".join(extra)
        prefix = "E" if self.kind == ENTITY_KIND else "R"
        return (
            f"{prefix}\t{self.type_label}\t{self.label}"
            f"\t{self.occurrence_count}{variant_suffix}"
        )


@dataclass(frozen=True)
class Inventory:
    """Deduplicated, source-text-free view of everything extracted so far."""

    entries: tuple[InventoryEntry, ...]

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def node_count(self) -> int:
        return sum(entry.occurrence_count for entry in self.entries)

    def render(self) -> str:
        return "\n".join(entry.render() for entry in self.entries)

    def cost(self) -> int:
        return estimate_cost(self.render())


def estimate_cost(text: str) -> int:
    """Context cost of ``text``, in characters.

    Characters rather than tokens: tokenization is model-specific, and a
    character budget is both deterministic and conservative for the mixed-script
    inputs this stage is meant to survive.
    """
    return len(text)


def entity_rows_to_items(rows: Iterable[object]) -> tuple[InventoryItem, ...]:
    """Adapt ``EntityRow``-shaped objects.  Duck-typed to stay import-light."""
    items: list[InventoryItem] = []
    for row in rows:
        items.append(
            InventoryItem(
                kind=ENTITY_KIND,
                node_id=str(getattr(row, "entity_id")),
                label=str(getattr(row, "display_name", "") or ""),
                type_label=str(getattr(row, "entity_type", "") or ""),
            )
        )
    return tuple(items)


def relationship_rows_to_items(rows: Iterable[object]) -> tuple[InventoryItem, ...]:
    """Adapt ``RelationshipRow``-shaped objects."""
    items: list[InventoryItem] = []
    for row in rows:
        items.append(
            InventoryItem(
                kind=RELATION_KIND,
                node_id=str(getattr(row, "relationship_id")),
                label=str(getattr(row, "relationship_type", "") or ""),
                type_label=str(getattr(row, "relationship_type", "") or ""),
            )
        )
    return tuple(items)


def build_inventory(items: Sequence[InventoryItem]) -> Inventory:
    """Collapse items into distinct entries, keeping every surface variant.

    Grouping is by ``(kind, type_label, normalize_label(label))`` — the same
    exact-match-after-normalization rule the live pipeline already applies.  The
    point is not to merge more here; it is to present what *did not* merge, with
    its variants, so the generalization step can act on it.
    """
    buckets: dict[tuple[str, str, str], list[InventoryItem]] = {}
    for item in items:
        normalized = normalize_label(item.label)
        buckets.setdefault((item.kind, item.type_label, normalized), []).append(item)

    entries: list[InventoryEntry] = []
    for (kind, type_label, normalized), grouped in buckets.items():
        variants = tuple(sorted({entry.label for entry in grouped}))
        entries.append(
            InventoryEntry(
                kind=kind,
                type_label=type_label,
                label=variants[0] if variants else normalized,
                normalized_label=normalized,
                node_ids=tuple(sorted(entry.node_id for entry in grouped)),
                variants=variants,
            )
        )
    entries.sort(key=lambda e: (e.kind, e.type_label, e.normalized_label))
    return Inventory(entries=tuple(entries))


@dataclass(frozen=True)
class InventoryBatch:
    """A subset of the inventory that fits the budget on its own."""

    entries: tuple[InventoryEntry, ...]

    def render(self) -> str:
        return "\n".join(entry.render() for entry in self.entries)

    def cost(self) -> int:
        return estimate_cost(self.render())


@dataclass(frozen=True)
class OversizedComponent:
    """A co-reference component that exceeds the budget by itself.

    Reported rather than split.  Splitting it would separate labels that share
    tokens, which is precisely the situation this module exists to prevent, so
    the caller must decide (raise the budget, or accept a lossy split).
    """

    entries: tuple[InventoryEntry, ...]
    cost: int


@dataclass(frozen=True)
class GeneralizationPlan:
    """How to present the inventory to a model under a context budget."""

    budget_chars: int
    inventory_cost: int
    batches: tuple[InventoryBatch, ...] = field(default_factory=tuple)
    oversized: tuple[OversizedComponent, ...] = field(default_factory=tuple)

    @property
    def fits_single_context(self) -> bool:
        return not self.oversized and len(self.batches) <= 1

    @property
    def batch_count(self) -> int:
        return len(self.batches)


def _components(entries: Sequence[InventoryEntry]) -> list[list[InventoryEntry]]:
    """Group entries that share a token, transitively.

    Co-referring surface variants nearly always share at least one token, so
    union-find over tokens keeps them in the same batch.  Entries are only ever
    linked within the same ``kind``; entities and relations are separate
    vocabularies and merging their token spaces would chain unrelated things
    together.
    """
    parent: dict[int, int] = {index: index for index in range(len(entries))}

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    first_seen: dict[tuple[str, str], int] = {}
    for index, entry in enumerate(entries):
        for token in label_tokens(entry.label):
            key = (entry.kind, token)
            if key in first_seen:
                union(first_seen[key], index)
            else:
                first_seen[key] = index

    grouped: dict[int, list[InventoryEntry]] = {}
    for index, entry in enumerate(entries):
        grouped.setdefault(find(index), []).append(entry)
    # Deterministic order: by the sort key of each component's first entry.
    return [grouped[root] for root in sorted(grouped)]


def co_reference_components(
    inventory: Inventory,
) -> tuple[tuple[InventoryEntry, ...], ...]:
    """Entries that share a token, transitively, as an over-merging ceiling.

    This is the loosest grouping the token vocabulary can justify: sharing one
    token is enough, and sharing chains transitively, so a common word merges
    everything that contains it.  It is therefore an **upper bound** on what a
    token-based identity policy could collapse, not a proposal.  Reported next
    to the live policy it brackets the answer: the number of genuinely distinct
    things lies between the two, and neither end is the truth.
    """
    return tuple(tuple(component) for component in _components(inventory.entries))


def plan_generalization(
    inventory: Inventory,
    *,
    budget_chars: int,
) -> GeneralizationPlan:
    """Decide whether the inventory fits one context, and partition if not.

    When it does not fit, batches are formed from co-reference components so
    that variants of the same thing stay together.  Components too large to fit
    alone are reported in :attr:`GeneralizationPlan.oversized` instead of being
    split silently.
    """
    if budget_chars <= 0:
        raise ValueError("budget_chars must be positive")

    total = inventory.cost()
    if not inventory.entries:
        return GeneralizationPlan(budget_chars=budget_chars, inventory_cost=0)
    if total <= budget_chars:
        return GeneralizationPlan(
            budget_chars=budget_chars,
            inventory_cost=total,
            batches=(InventoryBatch(entries=inventory.entries),),
        )

    batches: list[InventoryBatch] = []
    oversized: list[OversizedComponent] = []
    current: list[InventoryEntry] = []
    for component in _components(inventory.entries):
        component_cost = InventoryBatch(entries=tuple(component)).cost()
        if component_cost > budget_chars:
            oversized.append(
                OversizedComponent(entries=tuple(component), cost=component_cost)
            )
            continue
        candidate = current + component
        if current and InventoryBatch(entries=tuple(candidate)).cost() > budget_chars:
            batches.append(InventoryBatch(entries=tuple(current)))
            current = list(component)
        else:
            current = candidate
    if current:
        batches.append(InventoryBatch(entries=tuple(current)))

    return GeneralizationPlan(
        budget_chars=budget_chars,
        inventory_cost=total,
        batches=tuple(batches),
        oversized=tuple(oversized),
    )
