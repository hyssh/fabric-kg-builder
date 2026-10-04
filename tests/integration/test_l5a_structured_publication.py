from __future__ import annotations

from pathlib import Path

import pytest

from fabric_kg_builder.semantic.source_tables import require_l5_publication_receipt
from fabric_kg_builder.serving.structured_publication import run_l5a
from tests.unit.test_l5a_structured_publication import _FakeClient, _inputs


@pytest.mark.integration
@pytest.mark.offline
def test_l5a_full_local_pipeline_to_persisted_target_readback(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()

    result = run_l5a(
        **inputs,
        client=client,
        state_root=tmp_path / ".fkg" / "l5a",
    )

    require_l5_publication_receipt(inputs["source"], result)
    assert result.output_manifest.total_byte_count > 0
    assert result.output_manifest.total_row_count > 0
    assert result.metrics.fabric_calls == 12
    assert result.metrics.fabric_rows_written > 0
    assert result.metrics.fabric_rows_read == result.metrics.fabric_rows_written
