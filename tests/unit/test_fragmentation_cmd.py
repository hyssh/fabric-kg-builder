"""Tests for the measure-fragmentation command.

Domain-neutral by construction: every label here is a placeholder, and no test
asserts anything that depends on a particular subject area.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.fragmentation_cmd import (
    FragmentationInputError,
    build_report,
    measure_fragmentation_cmd,
    read_records,
    records_to_items,
)
from fabric_kg_builder.graph.generalization import ENTITY_KIND, InventoryItem


def _write_ndjson(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), "utf-8")
    return path


def _items(*pairs: tuple[str, str]) -> tuple[InventoryItem, ...]:
    return tuple(
        InventoryItem(kind=ENTITY_KIND, node_id=f"n{i}", label=label, type_label=type_label)
        for i, (label, type_label) in enumerate(pairs)
    )


# --- reading ---------------------------------------------------------------


def test_reads_ndjson(tmp_path: Path) -> None:
    path = _write_ndjson(tmp_path / "e.ndjson", [{"a": 1}, {"a": 2}])
    assert read_records([str(path)]) == [{"a": 1}, {"a": 2}]


def test_reads_json_array(tmp_path: Path) -> None:
    path = tmp_path / "e.json"
    path.write_text(json.dumps([{"a": 1}]), "utf-8")
    assert read_records([str(path)]) == [{"a": 1}]


def test_reads_nested_entities_key(tmp_path: Path) -> None:
    path = tmp_path / "e.json"
    path.write_text(json.dumps({"entities": [{"a": 1}]}), "utf-8")
    assert read_records([str(path)]) == [{"a": 1}]


def test_missing_file_is_an_input_error() -> None:
    with pytest.raises(FragmentationInputError):
        read_records(["/nonexistent/nowhere.json"])


# --- projection ------------------------------------------------------------


def test_rows_without_a_label_are_skipped_not_fatal() -> None:
    records = [
        {"entity_id": "a", "display_name": "Alpha", "entity_type": "T"},
        {"entity_id": "b", "display_name": "", "entity_type": "T"},
        {"entity_id": "c", "entity_type": "T"},
    ]
    items = records_to_items(
        records, id_field="entity_id", label_field="display_name", type_field="entity_type"
    )
    assert [i.label for i in items] == ["Alpha"]


def test_no_usable_labels_is_an_input_error() -> None:
    with pytest.raises(FragmentationInputError):
        records_to_items(
            [{"entity_id": "a"}],
            id_field="entity_id",
            label_field="display_name",
            type_field="entity_type",
        )


def test_duplicate_node_ids_are_disambiguated_so_the_denominator_stays_true() -> None:
    records = [
        {"entity_id": "same", "display_name": "Alpha", "entity_type": "T"},
        {"entity_id": "same", "display_name": "Beta", "entity_type": "T"},
    ]
    items = records_to_items(
        records, id_field="entity_id", label_field="display_name", type_field="entity_type"
    )
    assert len({i.node_id for i in items}) == 2
    assert build_report(items, context_budget=10_000)["node_count"] == 2


def test_missing_ids_are_synthesised_rather_than_dropping_the_row() -> None:
    items = records_to_items(
        [{"display_name": "Alpha", "entity_type": "T"}],
        id_field="entity_id",
        label_field="display_name",
        type_field="entity_type",
    )
    assert len(items) == 1


# --- measurement -----------------------------------------------------------


def test_baseline_reproduces_the_live_exact_match_policy() -> None:
    # Same label, same type, different case: the live policy already merges.
    report = build_report(_items(("Alpha", "T"), ("alpha", "T")), context_budget=10_000)
    assert report["baseline"]["group_count"] == 1
    assert report["baseline"]["fragmentation_rate"] == pytest.approx(0.5)


def test_same_label_under_different_types_is_not_merged() -> None:
    report = build_report(_items(("Alpha", "T1"), ("Alpha", "T2")), context_budget=10_000)
    assert report["baseline"]["group_count"] == 2
    assert report["baseline"]["fragmentation_rate"] == pytest.approx(0.0)


def test_candidate_merges_word_order_variants_the_baseline_leaves_apart() -> None:
    # Token-set equality is order-insensitive; the live normalized-string key
    # is not.  This is the one thing the candidate policy actually buys.
    report = build_report(
        _items(("Widget Z100", "T"), ("Z100 Widget", "T")), context_budget=10_000
    )
    assert report["baseline"]["group_count"] == 2, "baseline must expose the drift"
    assert report["candidate"]["group_count"] == 1
    assert report["disagreement"]["additional_merges"] == 1
    assert report["disagreement"]["additional_merge_rate"] == pytest.approx(0.5)


def test_candidate_does_not_merge_an_internal_hyphen_split() -> None:
    # "widget z-100" tokenizes to {widget, 100}: the one-character "z" falls
    # below the token floor, so the token sets differ from "Widget Z100".
    # Recorded because it is a real limit of the candidate policy, not a bug.
    report = build_report(
        _items(("Widget Z100", "T"), ("widget z-100", "T")), context_budget=10_000
    )
    assert report["candidate"]["group_count"] == 2
    assert report["ceiling"]["group_count"] == 1, "the ceiling does catch it"


def test_ceiling_is_reported_as_a_bound_not_a_proposal() -> None:
    report = build_report(_items(("Alpha", "T")), context_budget=10_000)
    note = report["ceiling"]["note"].lower()
    assert "upper bound" in note
    assert "not a proposal" in note


def test_ceiling_never_reports_more_groups_than_the_baseline() -> None:
    report = build_report(
        _items(("Widget Z100", "T"), ("widget z-100", "T"), ("Gadget", "T")),
        context_budget=10_000,
    )
    assert report["ceiling"]["group_count"] <= report["baseline"]["group_count"]
    assert report["ceiling"]["group_count"] == 2, "Gadget shares no token"


def test_ceiling_over_merges_on_a_shared_common_word() -> None:
    # The behaviour that makes it a bound rather than an answer.
    report = build_report(
        _items(("Common Alpha", "T"), ("Common Beta", "T")), context_budget=10_000
    )
    assert report["baseline"]["group_count"] == 2
    assert report["ceiling"]["group_count"] == 1


def test_disagreement_is_labelled_as_disagreement_not_improvement() -> None:
    report = build_report(_items(("Alpha", "T")), context_budget=10_000)
    note = report["disagreement"]["note"].lower()
    assert "not an improvement" in note
    assert "over-merge" in note


def test_non_latin_labels_survive_measurement() -> None:
    # The legacy normalizer maps these to the empty string; this path must not.
    report = build_report(
        _items(("한국어 부품", "T"), ("株式会社テスト", "T")), context_budget=10_000
    )
    assert report["node_count"] == 2
    assert report["candidate"]["group_count"] == 2, "distinct things stay distinct"


def test_distinct_cyrillic_labels_are_not_collapsed_onto_their_digits() -> None:
    # The legacy normalizer reduces both of these to "7" and false-merges them.
    report = build_report(
        _items(("Виджет 7", "T"), ("Деталь 7", "T")), context_budget=10_000
    )
    assert report["candidate"]["group_count"] == 2


def test_worst_groups_report_the_labels_behind_a_group() -> None:
    report = build_report(
        _items(("Alpha", "T"), ("alpha", "T"), ("Beta", "T")), context_budget=10_000
    )
    worst = report["baseline"]["worst_groups"][0]
    assert worst["node_count"] == 2
    assert worst["type"] == "T"
    assert worst["labels"] == ["Alpha", "alpha"]


def test_single_context_section_answers_the_budget_question() -> None:
    items = _items(*[(f"Label {i}", "T") for i in range(40)])
    generous = build_report(items, context_budget=100_000)["single_context"]
    assert generous["fits_single_context"] is True
    assert generous["batch_count"] == 1
    tight = build_report(items, context_budget=50)["single_context"]
    assert tight["fits_single_context"] is False
    assert tight["inventory_cost_chars"] > 50


# --- command ---------------------------------------------------------------


def test_command_reports_rates_and_writes_the_report(tmp_path: Path) -> None:
    src = _write_ndjson(
        tmp_path / "e.ndjson",
        [
            {"entity_id": "1", "display_name": "Widget Z100", "entity_type": "T"},
            {"entity_id": "2", "display_name": "widget z-100", "entity_type": "T"},
            {"entity_id": "3", "display_name": "Gadget", "entity_type": "T"},
        ],
    )
    out = tmp_path / "report.json"
    result = CliRunner().invoke(
        measure_fragmentation_cmd, [str(src), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "nodes                     3" in result.output
    report = json.loads(out.read_text("utf-8"))
    assert report["node_count"] == 3
    assert report["baseline"]["group_count"] == 3
    assert report["ceiling"]["group_count"] == 2
    assert "distinct things are between 2 and 3" in result.output


def test_command_does_not_fail_on_a_high_rate(tmp_path: Path) -> None:
    # A measurement command must report, never gate.
    src = _write_ndjson(
        tmp_path / "e.ndjson",
        [{"entity_id": str(i), "display_name": "Alpha", "entity_type": "T"} for i in range(20)],
    )
    result = CliRunner().invoke(
        measure_fragmentation_cmd, [str(src), "--out", str(tmp_path / "r.json")]
    )
    assert result.exit_code == 0
    assert "no threshold is applied" in result.output


def test_command_exits_1_on_missing_input(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        measure_fragmentation_cmd,
        [str(tmp_path / "nope.ndjson"), "--out", str(tmp_path / "r.json")],
    )
    assert result.exit_code == 1
    assert "error:" in result.output


def test_command_exits_1_when_no_record_carries_a_label(tmp_path: Path) -> None:
    src = _write_ndjson(tmp_path / "e.ndjson", [{"entity_id": "1"}])
    result = CliRunner().invoke(
        measure_fragmentation_cmd, [str(src), "--out", str(tmp_path / "r.json")]
    )
    assert result.exit_code == 1


def test_command_accepts_custom_field_names(tmp_path: Path) -> None:
    src = _write_ndjson(
        tmp_path / "e.ndjson",
        [{"nid": "1", "name": "Alpha", "kind": "K"}],
    )
    out = tmp_path / "r.json"
    result = CliRunner().invoke(
        measure_fragmentation_cmd,
        [str(src), "--out", str(out), "--id-field", "nid",
         "--label-field", "name", "--type-field", "kind"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text("utf-8"))["node_count"] == 1


def test_command_is_registered_on_the_cli() -> None:
    from fabric_kg_builder.cli.main import cli

    assert "measure-fragmentation" in cli.commands
