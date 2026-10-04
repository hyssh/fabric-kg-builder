from __future__ import annotations

from fabric_kg_builder.domain.stage import prepare_l1_stage
from tests.unit.test_l1_stage import _candidates, _preflight


def test_design_samples_are_bounded_and_not_corpus_authority(tmp_path) -> None:
    prepared = prepare_l1_stage(
        _preflight(tmp_path, "samples"),
        candidates=_candidates("samples"),
    )

    assert prepared.sample_manifest.sample_scope == "bounded_domain_design"
    assert prepared.source_profile.completeness_disclaimer == (
        "design samples are bounded proposal support, not the complete source universe"
    )
    assert prepared.preflight.corpus.inventory_scope == "complete"
    assert prepared.preflight.corpus.total_entry_count >= len(
        {item.source_file_id for item in prepared.sample_manifest.entries}
    )
    assert prepared.preflight.corpus.corpus_hash != prepared.sample_manifest.sample_hash
