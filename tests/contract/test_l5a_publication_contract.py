from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_kg_builder.contracts.publication import ProjectionEquivalenceV1_1
from fabric_kg_builder.contracts.receipts import ArtifactManifest, StageReceipt
from fabric_kg_builder.contracts.resources import (
    StageResourceMetrics,
    validate_receipt_resources,
)
from fabric_kg_builder.serving.structured_publication import L5A_TARGET_ORDER, run_l5a
from tests.unit.test_l5a_structured_publication import _FakeClient, _inputs


@pytest.mark.contract
def test_l5a_persisted_outputs_round_trip_through_c0_contracts(
    tmp_path: Path,
) -> None:
    result = run_l5a(
        **_inputs(tmp_path),
        client=_FakeClient(),
        state_root=tmp_path / ".fkg" / "l5a",
    )

    manifest = ArtifactManifest.model_validate_json(
        (result.run_root / "output-manifest.json").read_text("utf-8")
    )
    metrics = StageResourceMetrics.model_validate_json(
        (result.run_root / "resource-metrics.json").read_text("utf-8")
    )
    receipt = StageReceipt.model_validate_json(
        (result.run_root / "stage-receipt.json").read_text("utf-8")
    )
    proofs = tuple(
        ProjectionEquivalenceV1_1.model_validate(item)
        for item in json.loads(
            (result.run_root / "projection-equivalence.json").read_text("utf-8")
        )
    )

    validate_receipt_resources(receipt, metrics)
    assert manifest == result.output_manifest
    assert receipt == result.receipt
    assert proofs == result.projection_equivalences
    assert {proof.projection_kind for proof in proofs} == set(L5A_TARGET_ORDER)
    assert all(proof.authority.source_artifact_manifest_id for proof in proofs)
    assert all(proof.authority.source_artifact_manifest_hash for proof in proofs)
