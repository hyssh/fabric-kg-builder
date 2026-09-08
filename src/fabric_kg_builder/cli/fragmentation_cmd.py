"""measure-fragmentation command — corpus-scale identity fragmentation report.

Extraction runs per work unit, and the live identity policy merges two mentions
only when their business keys are identical after casefolding.  A thing named
slightly differently in two documents therefore becomes two nodes, permanently.
This command measures how much of that is present in an existing extraction
artifact, so a change to the identity policy can be shown to move a number.

It changes nothing.  It reads local files and writes a report.

Two groupings are computed over the same population:

* **baseline** — the live rule: type plus the label normalized by NFC,
  casefolding, and whitespace collapse.
* **candidate** — a looser, script-neutral rule: type plus the *set* of tokens
  in the label, so ``"Widget Z100"`` and ``"widget z-100"`` agree.

The difference between them is reported as disagreement, not as improvement.
A rule loose enough to merge everything maximises that number.  Telling a
correct merge from an over-merge requires labelled data; when a labelled
held-out domain exists, ``graph.metrics.b_cubed`` scores it.

Exit codes:
  0  measured successfully (whatever the rate)
  1  invalid input (missing file, unreadable, no usable records)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

import click

from fabric_kg_builder.graph.generalization import (
    ENTITY_KIND,
    InventoryItem,
    build_inventory,
    co_reference_components,
    label_tokens,
    normalize_label,
    plan_generalization,
)
from fabric_kg_builder.graph.metrics import compare_grouping, measure_grouping

_EPILOG = """\b
Example:
  fabric-kg measure-fragmentation build/parquet/entities.parquet
  fabric-kg measure-fragmentation extracted.ndjson --out build/fragmentation.json
  fabric-kg measure-fragmentation entities.parquet --context-budget 40000

Reports rates only; it never fails on a threshold. Exit codes: 0 measured - 1 invalid input.

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


class FragmentationInputError(ValueError):
    """Raised when the named files yield no usable entity records."""


def _parse_json_or_ndjson(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
        return records
    if isinstance(loaded, dict):
        for key in ("entities", "records", "value", "rows"):
            nested = loaded.get(key)
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
        return [loaded]
    if isinstance(loaded, list):
        return [row for row in loaded if isinstance(row, dict)]
    return []


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise FragmentationInputError(
            f"reading {path} requires pyarrow; install it or export the "
            f"artifact as JSON/NDJSON"
        ) from exc
    return pq.read_table(path).to_pylist()


def read_records(paths: Sequence[str]) -> list[dict[str, Any]]:
    """Read entity records from parquet or JSON/NDJSON files."""
    records: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise FragmentationInputError(f"not a file: {path}")
        if path.suffix.lower() == ".parquet":
            records.extend(_read_parquet(path))
        else:
            try:
                records.extend(_parse_json_or_ndjson(path.read_text("utf-8")))
            except json.JSONDecodeError as exc:
                raise FragmentationInputError(f"{path}: {exc}") from exc
    return records


def records_to_items(
    records: Sequence[dict[str, Any]],
    *,
    id_field: str,
    label_field: str,
    type_field: str,
) -> tuple[InventoryItem, ...]:
    """Project records onto inventory items, skipping rows without a label."""
    items: list[InventoryItem] = []
    for index, record in enumerate(records):
        label = record.get(label_field)
        if not isinstance(label, str) or not label.strip():
            continue
        node_id = record.get(id_field)
        if not isinstance(node_id, str) or not node_id:
            node_id = f"row-{index}"
        type_label = record.get(type_field)
        items.append(
            InventoryItem(
                kind=ENTITY_KIND,
                node_id=node_id,
                label=label,
                type_label=str(type_label) if isinstance(type_label, str) else "",
            )
        )
    if not items:
        raise FragmentationInputError(
            f"no records carried a non-empty {label_field!r} field"
        )
    # Node ids must be unique for the metric; disambiguate collisions rather
    # than dropping rows, so the denominator stays the true population.
    seen: dict[str, int] = {}
    unique: list[InventoryItem] = []
    for item in items:
        count = seen.get(item.node_id, 0)
        seen[item.node_id] = count + 1
        node_id = item.node_id if count == 0 else f"{item.node_id}#{count}"
        unique.append(
            InventoryItem(
                kind=item.kind,
                node_id=node_id,
                label=item.label,
                type_label=item.type_label,
            )
        )
    return tuple(unique)


def build_report(
    items: Sequence[InventoryItem],
    *,
    context_budget: int,
    worst_limit: int = 5,
) -> dict[str, Any]:
    """Measure both identity policies and the single-context budget question."""
    by_id = {item.node_id: item for item in items}
    node_ids = sorted(by_id)

    def baseline_key(node_id: str) -> tuple[str, str]:
        item = by_id[node_id]
        return (item.type_label, normalize_label(item.label))

    def candidate_key(node_id: str) -> tuple[str, frozenset[str]]:
        item = by_id[node_id]
        return (item.type_label, label_tokens(item.label))

    comparison = compare_grouping(
        node_ids, baseline_key=baseline_key, candidate_key=candidate_key
    )
    inventory = build_inventory(items)
    plan = plan_generalization(inventory, budget_chars=context_budget)

    # Over-merging upper bound: entries sharing any token, transitively.
    component_of: dict[str, int] = {}
    for index, component in enumerate(co_reference_components(inventory)):
        for entry in component:
            for node_id in entry.node_ids:
                component_of[node_id] = index
    ceiling = measure_grouping(node_ids, lambda n: component_of.get(n, n))

    def worst(report: Any) -> list[dict[str, Any]]:
        entries = []
        for key, members in report.worst_groups(worst_limit):
            entries.append(
                {
                    "type": key[0],
                    "node_count": len(members),
                    "labels": sorted({by_id[m].label for m in members}),
                }
            )
        return entries

    return {
        "node_count": comparison.baseline.node_count,
        "baseline": {
            "policy": "live exact-match identity (type + NFC/casefold/space)",
            "group_count": comparison.baseline.group_count,
            "fragmentation_rate": comparison.baseline.fragmentation_rate,
            "largest_group_size": comparison.baseline.largest_group_size,
            "worst_groups": worst(comparison.baseline),
        },
        "candidate": {
            "policy": "script-neutral token set (type + token set of label)",
            "group_count": comparison.candidate.group_count,
            "fragmentation_rate": comparison.candidate.fragmentation_rate,
            "largest_group_size": comparison.candidate.largest_group_size,
            "worst_groups": worst(comparison.candidate),
        },
        "ceiling": {
            "policy": "shared-token components (transitive; over-merging bound)",
            "group_count": ceiling.group_count,
            "fragmentation_rate": ceiling.fragmentation_rate,
            "largest_group_size": ceiling.largest_group_size,
            "note": (
                "Upper bound, not a proposal: one shared token is enough and "
                "sharing chains transitively, so a common word merges everything "
                "containing it. The number of genuinely distinct things lies "
                "between the baseline and this ceiling."
            ),
        },
        "disagreement": {
            "additional_merges": comparison.additional_merges,
            "additional_merge_rate": comparison.additional_merge_rate,
            "note": (
                "Disagreement between two policies, not an improvement. These "
                "merges may be correct or may be over-merges; distinguishing "
                "them needs labelled data (see graph.metrics.b_cubed)."
            ),
        },
        "single_context": {
            "budget_chars": context_budget,
            "inventory_cost_chars": plan.inventory_cost,
            "distinct_entries": inventory.entry_count,
            "fits_single_context": plan.fits_single_context,
            "batch_count": plan.batch_count,
            "oversized_component_count": len(plan.oversized),
        },
    }


def _format_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


@click.command(
    "measure-fragmentation",
    epilog=_EPILOG,
    context_settings={"max_content_width": 120},
)
@click.argument("files", nargs=-1, required=True, type=click.Path())
@click.option(
    "--out",
    default="build/fragmentation/report.json",
    show_default=True,
    type=click.Path(),
    help="Where to write the JSON report.",
)
@click.option(
    "--id-field", default="entity_id", show_default=True,
    help="Record field holding the node identifier.",
)
@click.option(
    "--label-field", default="display_name", show_default=True,
    help="Record field holding the human-readable label.",
)
@click.option(
    "--type-field", default="entity_type", show_default=True,
    help="Record field holding the entity type.",
)
@click.option(
    "--context-budget", default=40_000, show_default=True,
    type=click.IntRange(min=1),
    help="Character budget for presenting the inventory in one model context.",
)
def measure_fragmentation_cmd(
    files: tuple[str, ...],
    out: str,
    id_field: str,
    label_field: str,
    type_field: str,
    context_budget: int,
) -> None:
    """Report entity fragmentation rates for an extraction artifact."""
    try:
        records = read_records(files)
        items = records_to_items(
            records,
            id_field=id_field,
            label_field=label_field,
            type_field=type_field,
        )
    except FragmentationInputError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    report = build_report(items, context_budget=context_budget)
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), "utf-8")

    baseline = report["baseline"]
    candidate = report["candidate"]
    single = report["single_context"]
    click.echo(f"nodes                     {report['node_count']}")
    click.echo(
        f"baseline groups           {baseline['group_count']} "
        f"(fragmentation {_format_rate(baseline['fragmentation_rate'])})"
    )
    click.echo(
        f"candidate groups          {candidate['group_count']} "
        f"(fragmentation {_format_rate(candidate['fragmentation_rate'])})"
    )
    ceiling_out = report["ceiling"]
    click.echo(
        f"ceiling groups            {ceiling_out['group_count']} "
        f"(fragmentation {_format_rate(ceiling_out['fragmentation_rate'])}, over-merging bound)"
    )
    click.echo(
        f"distinct things are between {ceiling_out['group_count']} and "
        f"{baseline['group_count']}"
    )
    click.echo(
        f"policies disagree on      {report['disagreement']['additional_merges']} "
        f"nodes ({_format_rate(report['disagreement']['additional_merge_rate'])})"
    )
    click.echo(
        f"inventory                 {single['distinct_entries']} entries, "
        f"{single['inventory_cost_chars']} chars"
    )
    click.echo(
        f"fits one context          {single['fits_single_context']} "
        f"(budget {single['budget_chars']}, batches {single['batch_count']})"
    )
    if baseline["worst_groups"]:
        click.echo("\nlargest baseline groups (already merged):")
        for group in baseline["worst_groups"]:
            click.echo(f"  {group['node_count']:>4}  {group['type']}  {group['labels'][:3]}")
    click.echo(
        "\nRates only; no threshold is applied. Policy disagreement is not an "
        "improvement measure -- see the report's disagreement.note."
    )
    click.echo(f"report: {out_path}")
