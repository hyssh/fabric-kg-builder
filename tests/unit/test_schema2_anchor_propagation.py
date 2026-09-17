"""Anchor propagation across schema-2 work-unit splits (issue #135, defect C).

A work unit is a contiguous slice. When the relation budget forces a split, the
right-hand child starts partway through the parent span, so the heading that
gives its content meaning may no longer fall inside its slice. Before the fix,
exactly one leaf ever retained the governing anchor no matter how many splits
occurred, because the single-boundary overlap is positional, not semantic.

Every test here asserts the invariant we want -- each leaf carries its governing
anchor -- rather than pinning current behaviour, so the whole file goes green
when the fix is correct and red if it regresses.

Fixtures are deliberately domain-neutral placeholders: the defect is a property
of the splitter, not of any corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabric_kg_builder.contracts.evidence import SourceUnit
from fabric_kg_builder.contracts.identity import (
    CanonicalIdentityEnvelope,
    ImmutableSourceLocator,
)
from fabric_kg_builder.enrichment.schema2_work_units import (
    WorkUnitCheckpoint,
    execute_work_unit,
    root_work_unit,
    split_work_unit,
)

ANCHOR_TEXT = "Widget Z100 Parts Table"
ROW_MARKER = "PART-"


def _build_anchor_plus_table_text(*, row_count: int) -> str:
    """One governing heading followed by a dense, uniform table."""

    blocks = [ANCHOR_TEXT]
    for index in range(1, row_count + 1):
        blocks.append(f"Row {index:04d} {ROW_MARKER}{index:05d} RegionCode{index % 7}")
    return "\n\n".join(blocks)


def _source_unit(text: str) -> SourceUnit:
    identity = CanonicalIdentityEnvelope(
        contract_kind="c0.source_unit",
        project_id="project:anchor-propagation",
        asset_id="asset:test",
        asset_version_id="asset-version:test",
        run_id="run:anchor-propagation",
        source_file_id="source-file:test",
        source_unit_id=None,
        content_hash="a" * 64,
        domain_schema_version="2.0",
        domain_contract_hash="b" * 64,
        semantic_contract_hash=None,
        canonical_schema_version="2.0.0",
        prompt_version="l2-closed-vocabulary-v1",
        prompt_hash="c" * 64,
        model_version="test-model",
        model_hash="d" * 64,
        extractor_name="l2-schema-constrained",
        extractor_version="1.0.0",
        parent_artifact_ids=(),
        parent_record_ids=(),
        immutable_locator=None,
    )
    locator = ImmutableSourceLocator.from_authority(
        blob_uri="https://storage.example/source",
        blob_version_id="v1",
        char_start=0,
        char_end=len(text),
    )
    return SourceUnit.mint(
        identity=identity,
        unit_kind="paragraph",
        text=text,
        ordinal=0,
        locator=locator,
    )


class _OverflowService:
    """Reports an over-budget relation count for spans above a threshold.

    A dense table is exactly what trips the relation budget in production; this
    stand-in reproduces that trigger without any network access.
    """

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.calls = 0

    def complete(self, *, prompt: str, work_unit) -> dict:
        self.calls += 1
        relation_count = 5 if work_unit.coverage > self.threshold else 1
        return {
            "candidates": [
                {"candidate_kind": "relationship", "index": index}
                for index in range(relation_count)
            ]
        }


def _anchor_aware_processor(work_unit, response) -> dict:
    return {
        "slice_start": work_unit.slice_start,
        "slice_end": work_unit.slice_end,
        "row_count": work_unit.text.count(ROW_MARKER),
        # anchored_text is what the model actually receives.
        "has_anchor": ANCHOR_TEXT in work_unit.anchored_text,
    }


def _run(text: str, *, threshold: int, tmp_path: Path):
    root = root_work_unit(
        _source_unit(text),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )
    return execute_work_unit(
        root,
        service=_OverflowService(threshold),
        prompt_builder=lambda work_unit: work_unit.anchored_text,
        processor=_anchor_aware_processor,
        checkpoint=WorkUnitCheckpoint(
            tmp_path / "checkpoint.json",
            tmp_path / "leaves",
        ),
        max_relations_per_work_unit=2,
    ), root


def test_split_children_inherit_the_governing_anchor() -> None:
    root = root_work_unit(
        _source_unit(_build_anchor_plus_table_text(row_count=40)),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )

    children = split_work_unit(root)
    assert children is not None
    left, right = children

    # The right child starts partway through the table, so the heading is no
    # longer inside its slice -- it must be carried explicitly.
    assert ANCHOR_TEXT not in right.text
    assert ANCHOR_TEXT in right.anchored_text
    assert ANCHOR_TEXT in left.anchored_text


def test_every_leaf_bearing_table_rows_retains_its_anchor(tmp_path: Path) -> None:
    text = _build_anchor_plus_table_text(row_count=400)
    execution, root = _run(text, threshold=600, tmp_path=tmp_path)

    assert len(execution.leaf_results) > 5, (
        f"{len(execution.leaf_results)} leaves -- input must be large enough "
        "to force repeated splitting for this test to be meaningful"
    )
    # Splitting stays lossless.
    assert min(r["slice_start"] for r in execution.leaf_results) == 0
    assert max(r["slice_end"] for r in execution.leaf_results) == root.slice_end

    row_leaves = [r for r in execution.leaf_results if r["row_count"]]
    orphaned = [r for r in row_leaves if not r["has_anchor"]]

    assert not orphaned, (
        f"{len(orphaned)} of {len(row_leaves)} row-bearing leaves reached the "
        f"model with no trace of their governing anchor ({ANCHOR_TEXT!r}). "
        "Splitting must not separate content from the context it depends on."
    )


def test_anchor_is_not_duplicated_when_already_in_slice(tmp_path: Path) -> None:
    """The leaf that physically contains the heading must not repeat it."""

    text = _build_anchor_plus_table_text(row_count=120)
    execution, _ = _run(text, threshold=600, tmp_path=tmp_path)

    assert all(r["has_anchor"] for r in execution.leaf_results if r["row_count"])
    # Guard against fixing the invariant by blindly prefixing every slice.
    root = root_work_unit(
        _source_unit(text),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )
    children = split_work_unit(root)
    assert children is not None
    left, _ = children
    assert left.anchored_text.count(ANCHOR_TEXT) == 1


def test_small_input_that_needs_no_split_is_unaffected(tmp_path: Path) -> None:
    """Positive control: no split, no anchor machinery, output unchanged."""

    text = _build_anchor_plus_table_text(row_count=3)
    execution, _ = _run(text, threshold=100_000, tmp_path=tmp_path)

    assert len(execution.leaf_results) == 1
    leaf = execution.leaf_results[0]
    assert leaf["has_anchor"]
    assert leaf["row_count"] == 3


@pytest.mark.parametrize(
    ("row_count", "threshold"),
    [
        (20, 250),
        (20, 600),
        (60, 250),
        (60, 600),
        (60, 1200),
        (120, 600),
        (120, 1200),
        (120, 2500),
    ],
)
def test_anchor_survives_across_table_size_and_budget(
    tmp_path: Path, row_count: int, threshold: int
) -> None:
    """Raising the budget only delays splitting; the invariant must hold anyway.

    Any fixed threshold is eventually exceeded by a large enough table, so the
    anchor has to survive every configuration -- not just generous ones.
    """

    text = _build_anchor_plus_table_text(row_count=row_count)
    execution, _ = _run(text, threshold=threshold, tmp_path=tmp_path)

    if len(execution.leaf_results) == 1:
        pytest.skip("no split triggered for this row_count/threshold combination")

    row_leaves = [r for r in execution.leaf_results if r["row_count"]]
    orphaned = [r for r in row_leaves if not r["has_anchor"]]

    assert not orphaned, (
        f"row_count={row_count} threshold={threshold}: {len(orphaned)} of "
        f"{len(row_leaves)} row-bearing leaves lost their governing anchor"
    )
