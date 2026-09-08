"""Tests for the corpus-scale generalization stage and fragmentation metrics.

Everything here is offline and domain-neutral: placeholder labels only, no
vocabulary from any corpus the pipeline has been run against.
"""

from __future__ import annotations

import pytest

from fabric_kg_builder.graph.blocking import _normalize as legacy_normalize
from fabric_kg_builder.graph.generalization import (
    ENTITY_KIND,
    RELATION_KIND,
    Inventory,
    InventoryItem,
    build_inventory,
    entity_rows_to_items,
    estimate_cost,
    label_tokens,
    normalize_label,
    plan_generalization,
)
from fabric_kg_builder.graph.metrics import (
    b_cubed,
    compare_grouping,
    measure_grouping,
)


# ---------------------------------------------------------------------------
# normalize_label — script neutrality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    ["한국어 부품", "株式会社テスト", "Виджет", "Ωμέγα", "أداة"],
)
def test_non_latin_labels_survive_normalization(label: str) -> None:
    """The legacy normalizer erases these; generalization must not."""
    assert legacy_normalize(label).strip() in ("", "-")
    assert normalize_label(label) != ""


def test_distinct_cyrillic_labels_do_not_collapse_onto_their_digits() -> None:
    """Guards a false-merge hazard, not just a missed merge.

    The legacy normalizer reduces both of these to ``"7"``, which would give
    them identical blocking keys and an alias Jaccard of 1.0 — two unrelated
    entities merged into one.
    """
    assert legacy_normalize("Виджет 7") == legacy_normalize("Деталь 7")
    assert normalize_label("Виджет 7") != normalize_label("Деталь 7")


def test_punctuation_becomes_space_rather_than_being_deleted() -> None:
    """Deleting punctuation would make ``a-z`` collide with ``az``."""
    assert normalize_label("a-z") == "a z"
    assert normalize_label("a-z") != normalize_label("az")


def test_normalization_folds_case_and_collapses_whitespace() -> None:
    assert normalize_label("  Widget   Z100  ") == "widget z100"
    assert normalize_label("WIDGET z100") == normalize_label("widget Z100")


def test_normalization_of_punctuation_only_label_is_empty() -> None:
    assert normalize_label("--- ///") == ""


# ---------------------------------------------------------------------------
# label_tokens
# ---------------------------------------------------------------------------


def test_ideographic_labels_yield_bigrams_not_one_opaque_token() -> None:
    """Without bigrams a CJK label could never group with a variant of itself."""
    tokens = label_tokens("株式会社")
    assert "株式" in tokens
    assert len(tokens) > 1


def test_hangul_syllables_are_treated_as_ideographic() -> None:
    tokens = label_tokens("한국어")
    assert "한국" in tokens


def test_short_label_is_kept_verbatim_instead_of_vanishing() -> None:
    """Tokens below the length floor must not leave an entry ungroupable."""
    assert label_tokens("q") == frozenset({"q"})


def test_tokens_of_empty_label_are_empty() -> None:
    assert label_tokens("   ") == frozenset()


def test_latin_variants_share_a_token() -> None:
    """The property that lets a batch merge them later."""
    assert label_tokens("Widget Z100") & label_tokens("widget z-100")


# ---------------------------------------------------------------------------
# build_inventory
# ---------------------------------------------------------------------------


def _entity(node_id: str, label: str, type_label: str = "thing") -> InventoryItem:
    return InventoryItem(
        kind=ENTITY_KIND, node_id=node_id, label=label, type_label=type_label
    )


def test_inventory_collapses_labels_that_normalize_identically() -> None:
    inventory = build_inventory(
        [_entity("n1", "Widget Z100"), _entity("n2", "  widget   z100 ")]
    )
    assert inventory.entry_count == 1
    assert inventory.node_count == 2
    assert inventory.entries[0].occurrence_count == 2


def test_inventory_keeps_surface_variants_because_they_are_the_merge_signal() -> None:
    inventory = build_inventory(
        [_entity("n1", "Widget Z100"), _entity("n2", "widget z100")]
    )
    assert inventory.entries[0].variants == ("Widget Z100", "widget z100")
    assert "widget z100" in inventory.entries[0].render()


def test_inventory_does_not_merge_across_types() -> None:
    inventory = build_inventory(
        [_entity("n1", "Alpha", "kind-a"), _entity("n2", "Alpha", "kind-b")]
    )
    assert inventory.entry_count == 2


def test_inventory_does_not_merge_across_kinds() -> None:
    items = [
        _entity("n1", "Alpha", ""),
        InventoryItem(kind=RELATION_KIND, node_id="r1", label="Alpha"),
    ]
    assert build_inventory(items).entry_count == 2


def test_inventory_surfaces_what_the_exact_match_policy_leaves_separate() -> None:
    """``Z100`` and ``z-100`` normalize differently, so they stay separate.

    That is the fragmentation the stage exists to expose — the inventory must
    show it rather than quietly repair it.
    """
    inventory = build_inventory(
        [_entity("n1", "Widget Z100"), _entity("n2", "Widget z-100")]
    )
    assert inventory.entry_count == 2


def test_inventory_ordering_is_deterministic_regardless_of_input_order() -> None:
    items = [_entity("n1", "Beta"), _entity("n2", "Alpha"), _entity("n3", "Gamma")]
    forward = build_inventory(items).render()
    backward = build_inventory(list(reversed(items))).render()
    assert forward == backward


def test_inventory_node_ids_are_sorted() -> None:
    inventory = build_inventory([_entity("n9", "Alpha"), _entity("n1", "alpha")])
    assert inventory.entries[0].node_ids == ("n1", "n9")


def test_inventory_carries_no_source_text_only_labels_and_counts() -> None:
    """The size claim depends on this: cost must not scale with document size."""
    rendered = build_inventory([_entity("n1", "Alpha")]).render()
    assert rendered.count("\n") == 0
    assert estimate_cost(rendered) < 40


def test_item_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind must be"):
        InventoryItem(kind="nonsense", node_id="n1", label="Alpha")


def test_item_rejects_empty_node_id() -> None:
    with pytest.raises(ValueError, match="node_id"):
        InventoryItem(kind=ENTITY_KIND, node_id="", label="Alpha")


def test_entity_row_adapter_reads_duck_typed_rows() -> None:
    class Row:
        entity_id = "e1"
        display_name = "Alpha"
        entity_type = "thing"

    items = entity_rows_to_items([Row()])
    assert items[0].node_id == "e1"
    assert items[0].label == "Alpha"
    assert items[0].kind == ENTITY_KIND


# ---------------------------------------------------------------------------
# plan_generalization
# ---------------------------------------------------------------------------


def test_inventory_under_budget_is_one_batch() -> None:
    inventory = build_inventory([_entity(f"n{i}", f"Label {i}") for i in range(5)])
    plan = plan_generalization(inventory, budget_chars=10_000)
    assert plan.fits_single_context
    assert plan.batch_count == 1
    assert plan.batches[0].entries == inventory.entries


def test_inventory_over_budget_is_partitioned() -> None:
    inventory = build_inventory([_entity(f"n{i}", f"Label{i:04d}") for i in range(60)])
    plan = plan_generalization(inventory, budget_chars=200)
    assert not plan.fits_single_context
    assert plan.batch_count > 1


def test_every_batch_respects_the_budget() -> None:
    inventory = build_inventory([_entity(f"n{i}", f"Label{i:04d}") for i in range(60)])
    plan = plan_generalization(inventory, budget_chars=200)
    assert all(batch.cost() <= 200 for batch in plan.batches)


def test_partitioning_loses_no_entries() -> None:
    inventory = build_inventory([_entity(f"n{i}", f"Label{i:04d}") for i in range(60)])
    plan = plan_generalization(inventory, budget_chars=200)
    planned = [e for batch in plan.batches for e in batch.entries]
    planned += [e for component in plan.oversized for e in component.entries]
    assert sorted(e.normalized_label for e in planned) == sorted(
        e.normalized_label for e in inventory.entries
    )


def test_co_referring_variants_land_in_the_same_batch() -> None:
    """The property the whole partitioning scheme exists to preserve.

    A batch that separates two spellings of one thing cannot merge them, so
    naive chunking would defeat the stage no matter how capable the model is.
    """
    items = [_entity(f"pad{i}", f"Unrelated{i:04d}") for i in range(40)]
    items += [_entity("a", "Widget Z100"), _entity("b", "Widget z 100")]
    inventory = build_inventory(items)
    plan = plan_generalization(inventory, budget_chars=220)
    assert plan.batch_count > 1, "test is vacuous unless partitioning happened"
    located = [
        index
        for index, batch in enumerate(plan.batches)
        if any("widget" in e.normalized_label for e in batch.entries)
    ]
    assert len(located) == 1


def test_component_too_large_for_the_budget_is_reported_not_split() -> None:
    items = [_entity(f"n{i}", f"Shared Token Variant {i:04d}") for i in range(40)]
    plan = plan_generalization(build_inventory(items), budget_chars=120)
    assert plan.oversized
    assert not plan.fits_single_context


def test_empty_inventory_fits_and_has_no_batches() -> None:
    plan = plan_generalization(Inventory(entries=()), budget_chars=100)
    assert plan.fits_single_context
    assert plan.batch_count == 0
    assert plan.inventory_cost == 0


def test_non_positive_budget_is_rejected() -> None:
    inventory = build_inventory([_entity("n1", "Alpha")])
    with pytest.raises(ValueError, match="budget_chars"):
        plan_generalization(inventory, budget_chars=0)


def test_plan_is_deterministic() -> None:
    items = [_entity(f"n{i}", f"Label{i:04d}") for i in range(60)]
    first = plan_generalization(build_inventory(items), budget_chars=200)
    second = plan_generalization(
        build_inventory(list(reversed(items))), budget_chars=200
    )
    assert [b.render() for b in first.batches] == [b.render() for b in second.batches]


# ---------------------------------------------------------------------------
# measure_grouping / compare_grouping
# ---------------------------------------------------------------------------


def test_fragmentation_rate_is_zero_when_nothing_groups() -> None:
    report = measure_grouping(["a", "b", "c"], key=lambda n: n)
    assert report.fragmentation_rate == 0.0
    assert report.redundant_node_count == 0


def test_fragmentation_rate_counts_redundant_nodes() -> None:
    report = measure_grouping(["a", "b", "c", "d"], key=lambda n: n in ("a", "b"))
    assert report.group_count == 2
    assert report.fragmentation_rate == 0.5


def test_fragmentation_rate_is_one_when_everything_collapses() -> None:
    """Documents that a higher rate is not on its own an improvement."""
    report = measure_grouping(["a", "b", "c", "d"], key=lambda n: "same")
    assert report.fragmentation_rate == 0.75
    assert report.largest_group_size == 4


def test_fragmentation_rate_is_none_not_zero_for_an_empty_population() -> None:
    """An absent measurement must not read as a perfect score."""
    assert measure_grouping([], key=lambda n: n).fragmentation_rate is None


def test_duplicate_node_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        measure_grouping(["a", "a"], key=lambda n: n)


def test_worst_groups_ranks_largest_first_and_breaks_ties_stably() -> None:
    report = measure_grouping(
        ["a1", "a2", "a3", "b1", "b2", "c1"], key=lambda n: n[0]
    )
    assert [key for key, _ in report.worst_groups(2)] == ["a", "b"]


def test_comparison_reports_only_disagreement_between_two_policies() -> None:
    comparison = compare_grouping(
        ["a1", "a2", "b1", "b2"],
        baseline_key=lambda n: n,
        candidate_key=lambda n: n[0],
    )
    assert comparison.baseline.group_count == 4
    assert comparison.candidate.group_count == 2
    assert comparison.additional_merges == 2
    assert comparison.additional_merge_rate == 0.5


def test_comparison_rate_is_none_for_an_empty_population() -> None:
    comparison = compare_grouping(
        [], baseline_key=lambda n: n, candidate_key=lambda n: n
    )
    assert comparison.additional_merge_rate is None


# ---------------------------------------------------------------------------
# b_cubed
# ---------------------------------------------------------------------------


def test_b_cubed_is_perfect_when_clusterings_agree() -> None:
    clustering = {"a": 1, "b": 1, "c": 2}
    score = b_cubed(clustering, clustering)
    assert score.precision == 1.0
    assert score.recall == 1.0
    assert score.f1 == 1.0


def test_b_cubed_penalises_over_merging_through_precision() -> None:
    """A policy loose enough to merge everything scores well on recall only."""
    score = b_cubed({"a": 1, "b": 1, "c": 1}, {"a": 1, "b": 1, "c": 2})
    assert score.recall == 1.0
    assert score.precision < 1.0


def test_b_cubed_penalises_under_merging_through_recall() -> None:
    """This is our situation: exact-match identity splits one thing into many."""
    score = b_cubed({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 1, "c": 1})
    assert score.precision == 1.0
    assert score.recall < 1.0


def test_b_cubed_requires_the_same_element_universe() -> None:
    with pytest.raises(ValueError, match="same elements"):
        b_cubed({"a": 1}, {"a": 1, "b": 1})


def test_b_cubed_of_nothing_is_zero_not_one() -> None:
    score = b_cubed({}, {})
    assert score.f1 == 0.0
