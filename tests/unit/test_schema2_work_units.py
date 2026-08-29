from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_kg_builder.contracts.evidence import SourceUnit
from fabric_kg_builder.contracts.identity import (
    CanonicalIdentityEnvelope,
    ImmutableSourceLocator,
)
from fabric_kg_builder.enrichment.schema2_sources import L2StageError
from fabric_kg_builder.enrichment.schema2_work_units import (
    WorkUnitCheckpoint,
    execute_work_unit,
    root_work_unit,
    split_work_unit,
)


def _source_unit(text: str) -> SourceUnit:
    identity = CanonicalIdentityEnvelope(
        contract_kind="c0.source_unit",
        project_id="project:l2-tests",
        asset_id="asset:test",
        asset_version_id="asset-version:test",
        run_id="run:l2-tests",
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


class _BoundedService:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, prompt: str, work_unit) -> dict:
        self.calls += 1
        relation_count = 3 if work_unit.coverage > 30 else 1
        return {
            "candidates": [
                {"candidate_kind": "relationship", "index": index}
                for index in range(relation_count)
            ]
        }


def _processor(work_unit, response) -> dict:
    return {
        "slice_start": work_unit.slice_start,
        "slice_end": work_unit.slice_end,
        "candidate_count": len(response["candidates"]),
    }


def test_split_is_deterministic_and_preserves_complete_coverage() -> None:
    root = root_work_unit(
        _source_unit("First sentence. Second sentence. Third sentence."),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )

    first = split_work_unit(root)
    second = split_work_unit(root)

    assert first == second
    assert first is not None
    assert first[0].slice_start == root.slice_start
    assert first[1].slice_end == root.slice_end
    assert first[0].slice_end >= first[1].slice_start
    assert all(child.coverage < root.coverage for child in first)


def test_over_budget_parent_is_discarded_and_children_are_not_truncated(
    tmp_path: Path,
) -> None:
    root = root_work_unit(
        _source_unit(
            "Facility A contains Pump 1. "
            "Facility B contains Pump 2. "
            "Facility C contains Pump 3."
        ),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )
    service = _BoundedService()

    execution = execute_work_unit(
        root,
        service=service,
        prompt_builder=lambda work_unit: work_unit.text,
        processor=_processor,
        checkpoint=WorkUnitCheckpoint(
            tmp_path / "checkpoint.json",
            tmp_path / "leaves",
        ),
        max_relations_per_work_unit=2,
    )

    assert len(execution.leaf_results) >= 2
    assert all(item["candidate_count"] <= 2 for item in execution.leaf_results)
    assert min(item["slice_start"] for item in execution.leaf_results) == 0
    assert max(item["slice_end"] for item in execution.leaf_results) == root.slice_end
    assert execution.model_call_count == service.calls

    resumed_service = _BoundedService()
    resumed = execute_work_unit(
        root,
        service=resumed_service,
        prompt_builder=lambda work_unit: work_unit.text,
        processor=_processor,
        checkpoint=WorkUnitCheckpoint(
            tmp_path / "checkpoint.json",
            tmp_path / "leaves",
        ),
        max_relations_per_work_unit=2,
    )
    assert resumed.leaf_results == execution.leaf_results
    assert resumed.model_call_count == 0
    assert resumed_service.calls == 0


def test_successful_leaves_are_reused_without_remote_work(tmp_path: Path) -> None:
    root = root_work_unit(
        _source_unit("Facility A contains Pump 1."),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    artifact_dir = tmp_path / "leaves"
    first_service = _BoundedService()
    first = execute_work_unit(
        root,
        service=first_service,
        prompt_builder=lambda work_unit: work_unit.text,
        processor=_processor,
        checkpoint=WorkUnitCheckpoint(checkpoint_path, artifact_dir),
        max_relations_per_work_unit=2,
    )
    second_service = _BoundedService()
    second = execute_work_unit(
        root,
        service=second_service,
        prompt_builder=lambda work_unit: work_unit.text,
        processor=_processor,
        checkpoint=WorkUnitCheckpoint(checkpoint_path, artifact_dir),
        max_relations_per_work_unit=2,
    )

    assert first.leaf_results == second.leaf_results
    assert second.model_call_count == 0
    assert second.reused_leaf_count == len(second.leaf_results)
    assert second_service.calls == 0


def test_late_duplicate_writer_reuses_first_committed_leaf(
    tmp_path: Path,
) -> None:
    root = root_work_unit(
        _source_unit("Facility A contains Pump 1."),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )
    checkpoint = WorkUnitCheckpoint(
        tmp_path / "checkpoint.json",
        tmp_path / "leaves",
    )
    checkpoint.record_leaf(root, {"candidate_count": 1})
    checkpoint.record_leaf(root, {"candidate_count": 2})

    assert checkpoint.reuse(root) == {"candidate_count": 1}


def test_corrupt_leaf_is_rerun_and_repaired(tmp_path: Path) -> None:
    root = root_work_unit(
        _source_unit("Facility A contains Pump 1."),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    artifact_dir = tmp_path / "leaves"
    execute_work_unit(
        root,
        service=_BoundedService(),
        prompt_builder=lambda work_unit: work_unit.text,
        processor=_processor,
        checkpoint=WorkUnitCheckpoint(checkpoint_path, artifact_dir),
        max_relations_per_work_unit=2,
    )
    artifact = next(artifact_dir.glob("*.json"))
    artifact.write_text("{corrupt", encoding="utf-8")
    service = _BoundedService()

    rerun = execute_work_unit(
        root,
        service=service,
        prompt_builder=lambda work_unit: work_unit.text,
        processor=_processor,
        checkpoint=WorkUnitCheckpoint(checkpoint_path, artifact_dir),
        max_relations_per_work_unit=2,
    )

    assert rerun.model_call_count == 1
    assert service.calls == 1
    assert json.loads(artifact.read_text(encoding="utf-8"))


def test_indivisible_overflow_fails_without_partial_result(tmp_path: Path) -> None:
    root = root_work_unit(
        _source_unit("indivisible"),
        pass_name="candidate-extraction",
        authority_fingerprint="a" * 64,
    )

    with pytest.raises(L2StageError, match="indivisible work unit"):
        execute_work_unit(
            root,
            service=_BoundedService(),
            prompt_builder=lambda work_unit: work_unit.text,
            processor=_processor,
            checkpoint=WorkUnitCheckpoint(
                tmp_path / "checkpoint.json",
                tmp_path / "leaves",
            ),
            max_relations_per_work_unit=0,
        )

    assert not (tmp_path / "leaves").exists()
