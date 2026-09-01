"""Executed reproduction of defect (C) — batch/split-boundary context loss —
on the *default* (schema-2) extraction path.

Background: an earlier code-level trace (see issue #135) hypothesized that
context (a governing device heading) can separate from the content that
depends on it (a large parts/SKU table) during extraction batching. That
trace was first written against ``enrichment/orchestrator.py``, which turned
out to be the *legacy fallback* path (used only for non-V2 domain contracts),
not the default. The default path for an approved ``domain.yaml`` is
schema-2, via ``enrichment/schema2_work_units.py::execute_work_unit`` /
``split_work_unit``.

This test drives that real schema-2 code directly with a stubbed
``CandidateModelService`` (no network/cloud access — the stub simply reports
an over-budget relationship count for large slices, which is exactly the
condition ``execute_work_unit`` uses to decide to split). It is a synthetic,
domain-neutral repro: the "device" and "table" here are placeholder text with
no product-specific content, chosen only to have the right *shape* (one
heading followed by a table much larger than the relation budget).

This test is expected to demonstrate the defect (i.e. to fail on current
`main`/this branch, absent a fix): most of the leaf work units produced from
the oversized table lose all trace of the governing heading they depend on,
because ``split_work_unit`` selects a boundary nearest the arithmetic
midpoint (paragraph/sentence/whitespace, in that priority order) with only a
single boundary of overlap, and carries no heading recap forward when a
table-sized region must itself be split repeatedly.

Do not "fix" this test by loosening its assertions to match current
behavior — its purpose is to fail until the anchor-preservation invariant
proposed in issue #135 is implemented, at which point it should pass.
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
)

# A stand-in "governing anchor" — analogous to a device heading that
# identifies what a following parts/SKU table belongs to. Deliberately
# generic/non-domain-specific text; only its *presence or absence* in a
# work unit's text is checked, never its content.
ANCHOR_TEXT = "Widget Z100 Parts Table"

# Threshold above which the stub service reports an over-budget relationship
# count, forcing execute_work_unit to invoke split_work_unit. Chosen well
# below the size of the synthetic table so the table portion must itself be
# split multiple times.
_OVERFLOW_COVERAGE_THRESHOLD = 250


def _source_unit(text: str) -> SourceUnit:
    identity = CanonicalIdentityEnvelope(
        contract_kind="c0.source_unit",
        project_id="project:l2-context-loss-repro",
        asset_id="asset:test",
        asset_version_id="asset-version:test",
        run_id="run:l2-context-loss-repro",
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


def _build_anchor_plus_table_text(row_count: int) -> str:
    """One governing-anchor paragraph, then a large single-block table.

    The blank line after the anchor is the *only* paragraph boundary in the
    text, so the first split cleanly separates the anchor from the table
    (this mirrors DI assigning a heading its own element, then a large table
    as a separate but dependent element). Every row after that is joined by
    a single newline (no blank lines, no sentence-ending punctuation), so
    any further split of the table portion has no paragraph or sentence
    boundary to use and falls through to raw whitespace boundaries — the
    weakest, least structure-aware option ``_candidate_boundaries`` offers.
    """

    rows = "\n".join(
        f"Row {index:04d} PART-{index:05d} RegionCode{index % 11}"
        for index in range(1, row_count + 1)
    )
    return f"{ANCHOR_TEXT}\n\n{rows}"


class _RelationBudgetOverflowService:
    """Stub CandidateModelService: no network calls.

    Reports a relationship count above the configured budget for any slice
    larger than ``_OVERFLOW_COVERAGE_THRESHOLD`` characters, and at/under
    budget otherwise. This is the exact condition
    ``execute_work_unit`` checks (``_relationship_count(response) >
    max_relations_per_work_unit``) to decide whether to call
    ``split_work_unit`` — so this stub forces the real split logic to run
    repeatedly on the synthetic table without any cloud/model access.
    """

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, prompt: str, work_unit) -> dict:
        self.calls += 1
        over_budget = work_unit.coverage > _OVERFLOW_COVERAGE_THRESHOLD
        relation_count = 5 if over_budget else 1
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
        "coverage": work_unit.coverage,
        "has_anchor": ANCHOR_TEXT in work_unit.text,
        "text_preview": work_unit.text[:48],
    }


def test_oversized_table_split_orphans_leaves_from_their_governing_anchor(
    tmp_path: Path,
) -> None:
    """Executed repro: schema-2 splitting loses the anchor for most leaves.

    This is the schema-2 counterpart to the legacy-path repro already
    recorded on issue #135 (1 heading + 800 rows -> 457/800 rows, 57.1%,
    reached the model with no device anchor, via ``orchestrator.py``'s
    character-budget flush). Here the same *class* of outcome is produced
    through the *default* pipeline's relation-budget split, using only
    ``execute_work_unit``/``split_work_unit`` from
    ``enrichment/schema2_work_units.py`` — no orchestrator, no CLI, no I/O
    beyond the checkpoint files pytest's tmp_path provides.
    """

    source_text = _build_anchor_plus_table_text(row_count=400)
    root = root_work_unit(
        _source_unit(source_text),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )

    service = _RelationBudgetOverflowService()
    execution = execute_work_unit(
        root,
        service=service,
        prompt_builder=lambda work_unit: work_unit.text,
        processor=_anchor_aware_processor,
        checkpoint=WorkUnitCheckpoint(
            tmp_path / "checkpoint.json",
            tmp_path / "leaves",
        ),
        max_relations_per_work_unit=2,
    )

    # Sanity: the split mechanism actually engaged (this is not a no-split
    # trivial case) and coverage is complete/contiguous, matching the
    # existing test_over_budget_parent_is_discarded_and_children_are_not_truncated
    # invariant this repro depends on.
    assert len(execution.leaf_results) > 5, (
        "expected the oversized table to force multiple splits; got "
        f"{len(execution.leaf_results)} leaves — repro input may need to "
        "be larger relative to _OVERFLOW_COVERAGE_THRESHOLD"
    )
    assert min(r["slice_start"] for r in execution.leaf_results) == 0
    assert max(r["slice_end"] for r in execution.leaf_results) == root.slice_end

    anchored = [r for r in execution.leaf_results if r["has_anchor"]]
    orphaned = [r for r in execution.leaf_results if not r["has_anchor"]]

    orphaned_fraction = len(orphaned) / len(execution.leaf_results)
    print(
        f"\n[defect-C schema-2 repro] {len(orphaned)}/{len(execution.leaf_results)} "
        f"leaves ({orphaned_fraction:.1%}) carry no trace of their governing "
        f"anchor ({ANCHOR_TEXT!r}). First orphaned leaf preview: "
        f"{orphaned[0]['text_preview']!r}" if orphaned else "none orphaned"
    )

    # THE DEFECT ASSERTION. Every leaf that depends on the anchor for correct
    # extraction should carry it — either directly in its own text, or (once
    # a fix lands) via a carried-forward recap. Today, splitting the table
    # away from its heading via a single paragraph boundary, then repeatedly
    # halving the remaining table text on bare whitespace boundaries with
    # only one boundary of overlap, guarantees the anchor is present in at
    # most the first table-adjacent leaf and absent from the rest.
    #
    # This assertion is expected to FAIL on current code (anchor lost for
    # the large majority of leaves) and is expected to PASS once
    # split_work_unit (or a wrapping stage) is changed to keep a heading
    # recap attached to every leaf that descends from an anchored region.
    assert not orphaned, (
        f"defect (C) reproduced on the schema-2 split path: {len(orphaned)} of "
        f"{len(execution.leaf_results)} leaves ({orphaned_fraction:.1%}) were "
        "extracted with no trace of their governing anchor text. This "
        "confirms (executed, not just code-traced) that split_work_unit's "
        "single-boundary overlap and midpoint-nearest boundary selection do "
        "not guarantee anchor context travels with dependent content, "
        "exactly as described in issue #135 defect (C) for the default "
        "(schema-2) extraction path."
    )


def test_small_anchor_plus_table_that_fits_budget_keeps_anchor(
    tmp_path: Path,
) -> None:
    """Positive control: when no split is needed, the anchor is naturally
    present in the single leaf. This isolates the defect to the *split*
    path specifically, rather than something wrong with anchor detection
    or work-unit construction in general.
    """

    source_text = _build_anchor_plus_table_text(row_count=3)
    root = root_work_unit(
        _source_unit(source_text),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )

    service = _RelationBudgetOverflowService()
    execution = execute_work_unit(
        root,
        service=service,
        prompt_builder=lambda work_unit: work_unit.text,
        processor=_anchor_aware_processor,
        checkpoint=WorkUnitCheckpoint(
            tmp_path / "checkpoint.json",
            tmp_path / "leaves",
        ),
        max_relations_per_work_unit=2,
    )

    assert len(execution.leaf_results) == 1, "small input should not split"
    assert execution.leaf_results[0]["has_anchor"]
