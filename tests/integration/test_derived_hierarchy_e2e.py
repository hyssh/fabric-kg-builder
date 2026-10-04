"""Unmocked native end-to-end proof: sealed L4 -> native Leiden -> L5a.

Skips when graspologic-native is unavailable (e.g. Python >= 3.14). No LLM,
Azure or Fabric calls; Fabric targets are the in-process fake client.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

pytest.importorskip("graspologic_native")

from fabric_kg_builder.cli import derive_hierarchy_cmd  # noqa: E402
from fabric_kg_builder.cli.main import cli  # noqa: E402
from fabric_kg_builder.graph.community_hierarchy_v2 import (  # noqa: E402
    build_derived_community_hierarchy_safe,
)
from fabric_kg_builder.graph.community_provider import NativeLeidenProvider  # noqa: E402
from fabric_kg_builder.semantic.derived_hierarchy_artifact import (  # noqa: E402
    write_derived_hierarchy_artifact,
)
from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import (  # noqa: E402
    build_hierarchy_core_rows,
    read_sealed_l4_hierarchy_rows,
)
from fabric_kg_builder.serving import l5a_hierarchy  # noqa: E402
from fabric_kg_builder.serving.structured_publication import (  # noqa: E402
    compile_l5a_publication,
)
from tests.unit.test_derive_hierarchy_cmd import _run_pipeline  # noqa: E402
from tests.unit.test_l5a_hierarchy_publication import _publication_inputs  # noqa: E402
from tests.unit.test_l5a_structured_publication import _inputs  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.offline]

_COMMUNITY_TABLES = ("graph_communities", "graph_community_members")


def _assert_membership_conserved(members: list[dict], entity_ids: set[str]) -> None:
    assert {row["entity_id"] for row in members} == entity_ids


def test_native_cli_then_unpatched_l5a_loader(tmp_path: Path) -> None:
    assert derive_hierarchy_cmd._test_only_provider_factory is None
    assert l5a_hierarchy._source_core_input.__module__ == l5a_hierarchy.__name__
    l3, l4 = _run_pipeline(tmp_path)
    out = tmp_path / "derived"
    result = CliRunner().invoke(cli, [
        "derive-hierarchy",
        "--source", str(l4.run_root),
        "--out", str(out),
        "--input-manifest-search-root", str(l3.state_root),
        "--run-id", "e2e",
        "--policy-id", "e2e-policy",
        "--l1-state", str(tmp_path / "x1"),
        "--l2-state", str(tmp_path / "x2"),
        "--l3-state", str(tmp_path / "x3"),
        "--l4-state", str(tmp_path / "x4"),
        "--l5a-release-dir", str(tmp_path / "xr"),
    ])
    assert result.exit_code == 0, result.output

    receipt = json.loads((out / "derived-hierarchy-receipt.json").read_text())
    assert receipt["status"] == "complete"
    assert receipt["realized_provider_id"] == NativeLeidenProvider.provider_id

    source = l4.sealed_source()
    core = build_hierarchy_core_rows(read_sealed_l4_hierarchy_rows(source))
    assert core.input_graph_hash == receipt["input_graph_hash"]

    tables, _binding = l5a_hierarchy.load_hierarchy_tables(source, out)
    assert tables["graph_communities"].num_rows > 0
    _assert_membership_conserved(
        tables["graph_community_members"].to_pylist(),
        {entity.entity_id for entity in core.entities},
    )


def test_real_helper_native_writer_compile(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path / "source", member_count=4, extra_relationship_targets=True)
    source = inputs["source"]
    sealed = read_sealed_l4_hierarchy_rows(source)
    core = build_hierarchy_core_rows(sealed)
    entity_evidence = {e.entity_id: tuple(e.evidence_ids or ()) for e in core.entities}
    relationship_evidence = {
        r.relationship_id: tuple(r.evidence_ids or ()) for r in core.relationships
    }
    identity = dict(
        run_id="e2e", config_id="c", policy_id="p", seed=7, max_cluster_size=10,
    )
    outcome = build_derived_community_hierarchy_safe(
        list(core.entities), list(core.relationships),
        provider=NativeLeidenProvider(),
        entity_evidence=entity_evidence, relationship_evidence=relationship_evidence,
        **identity,
    )
    assert outcome.status.value == "complete"
    assert outcome.input_graph_hash == core.input_graph_hash

    out = tmp_path / "derived"
    write_derived_hierarchy_artifact(
        sealed=sealed, outcome=outcome, output_root=out,
        requested_provider_name=NativeLeidenProvider.provider_id,
        requested_provider_version=NativeLeidenProvider.provider_version,
        entity_evidence=entity_evidence, relationship_evidence=relationship_evidence,
        **identity,
    )
    receipt = json.loads((out / "derived-hierarchy-receipt.json").read_text())
    assert receipt["input_graph_hash"] == core.input_graph_hash
    assert receipt["execution_fingerprint"] == outcome.execution_fingerprint

    compiled = compile_l5a_publication(
        source, **_publication_inputs(inputs, out), hierarchy_state=out,
    )
    communities = compiled.tables["graph_communities"].to_pylist()
    members = compiled.tables["graph_community_members"].to_pylist()
    assert communities and members
    assert {row["level"] for row in communities} >= {0}
    _assert_membership_conserved(members, {e.entity_id for e in core.entities})

    baseline = compile_l5a_publication(source, **_publication_inputs(inputs))
    for name in _COMMUNITY_TABLES:
        assert name not in baseline.tables
