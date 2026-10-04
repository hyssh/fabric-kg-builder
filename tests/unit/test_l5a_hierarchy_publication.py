"""Offline publication proof; scripted partitions are not native Leiden quality proof."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.app_cmd import app_cmd
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.graph.community_hierarchy_v2 import build_derived_community_hierarchy
from fabric_kg_builder.graph.community_provider_testing import DeterministicConnectedComponentsProvider
from fabric_kg_builder.graph.community_provider import NativeLeidenProvider
from fabric_kg_builder.semantic.derived_hierarchy_artifact import write_derived_hierarchy_artifact
from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import (
    build_hierarchy_core_rows,
    read_sealed_l4_hierarchy_rows,
)
from fabric_kg_builder.serving import l5a_hierarchy
from fabric_kg_builder.serving.l5a_crosswalk import compile_governed_assets
from fabric_kg_builder.serving.structured_publication import (
    L5aPublicationError,
    compile_l5a_publication,
    l5a_input_fingerprint,
    require_l5a_publication_receipt,
    run_l5a,
)
from tests.unit.test_l5a_structured_publication import _FakeClient, _inputs

WORKSPACE = "00000000-0000-0000-0000-0000000000ff"
_PRODUCTION_CORE_INPUT = l5a_hierarchy._source_core_input


class _ScriptedNativeRows(DeterministicConnectedComponentsProvider):
    """Test partition rows with production artifact identity, not native execution."""

    provider_id = NativeLeidenProvider.provider_id
    provider_version = NativeLeidenProvider.provider_version


@pytest.fixture
def hierarchy_input(tmp_path):
    inputs = _inputs(tmp_path / "source")
    source = inputs["source"]
    sealed = read_sealed_l4_hierarchy_rows(source)
    # Production shared conversion (W3); no fake core-input seam.
    core_input = build_hierarchy_core_rows(sealed)
    entities = list(core_input.entities)
    relationships = list(core_input.relationships)
    entity_evidence = {entity.entity_id: tuple(entity.evidence_ids or ()) for entity in entities}
    relationship_evidence = {
        row.relationship_id: tuple(row.evidence_ids or ()) for row in relationships
    }
    outcome = build_derived_community_hierarchy(
        entities, relationships, provider=_ScriptedNativeRows(),
        run_id="hierarchy-run", config_id="hierarchy-config",
        policy_id="hierarchy-policy", seed=42, max_cluster_size=100,
        entity_evidence=entity_evidence, relationship_evidence=relationship_evidence,
    )
    assert outcome.input_graph_hash == core_input.input_graph_hash
    root = tmp_path / "hierarchy"
    write_derived_hierarchy_artifact(
        sealed=sealed, outcome=outcome, output_root=root,
        run_id="hierarchy-run", config_id="hierarchy-config",
        policy_id="hierarchy-policy", seed=42, max_cluster_size=100,
        requested_provider_name=outcome.provider_id,
        requested_provider_version=outcome.provider_version,
        entity_evidence=entity_evidence, relationship_evidence=relationship_evidence,
    )
    return inputs, root, outcome


def _publication_inputs(inputs, root=None):
    values = {key: value for key, value in inputs.items() if key != "source"}
    if root is not None:
        values["governed_assets"] = compile_governed_assets(
            inputs["source"], crosswalks=inputs["crosswalks"],
            access_policy=inputs["access_policy"], target_ids=inputs["target_ids"],
            workspace_id=WORKSPACE, hierarchy_state=root,
        )
    return values


def _reseal(root, *, table_name=None, mutate=None, receipt_updates=None):
    """Reseal deliberately invalid semantic rows to test checks beyond byte hashes."""
    receipt_path = root / "derived-hierarchy-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    if table_name is not None:
        table_path = root / f"{table_name.replace('_', '-')}.parquet"
        table = pq.read_table(table_path)
        rows = table.to_pylist()
        mutate(rows)
        for row in rows:
            row["row_hash"] = canonical_sha256({
                key: value for key, value in row.items() if key != "row_hash"
            })
        import pyarrow as pa

        pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), table_path)
        payload = table_path.read_bytes()
        manifest_path = root / "derived-hierarchy-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        kind = l5a_hierarchy.DERIVED_HIERARCHY_CONTRACT_KINDS[table_name]
        entry = next(item for item in manifest["entries"] if item["contract_kind"] == kind)
        entry.update(content_hash=hashlib.sha256(payload).hexdigest(), byte_count=len(payload))
        manifest["total_byte_count"] = sum(item["byte_count"] for item in manifest["entries"])
        manifest["manifest_hash"] = canonical_sha256({
            key: value for key, value in manifest.items() if key != "manifest_hash"
        })
        manifest_path.write_text(json.dumps(manifest))
        receipt["output_manifest_hash"] = manifest["manifest_hash"]
    receipt.update(receipt_updates or {})
    receipt["receipt_hash"] = canonical_sha256({
        key: value for key, value in receipt.items() if key != "receipt_hash"
    })
    receipt_path.write_text(json.dumps(receipt))


def test_compile_publishes_canonical_members_and_descriptions(hierarchy_input):
    inputs, root, outcome = hierarchy_input
    compiled = compile_l5a_publication(
        inputs["source"], **_publication_inputs(inputs, root), hierarchy_state=root,
    )
    communities = compiled.tables["graph_communities"].to_pylist()
    assert {row["community_id"] for row in communities} == {row.cluster_id for row in outcome.clusters}
    assert sum(row["size"] for row in communities if row["is_terminal"]) == len(outcome.memberships)
    rows = compiled.tables["graph_community_members"].to_pylist()
    assert {row["entity_id"] for row in rows} == set(outcome.projection.node_ids)
    expected_types = {
        (row["entity_id"], row["semantic_type_id"])
        for row in pq.read_table(inputs["source"].resolve("semantic_entity_type_assertions")).to_pylist()
    }
    assert {(row["entity_id"], row["entity_type"]) for row in rows} == expected_types
    assert all(row["level"] == 0 for row in rows)
    assert compiled.tables["graph_communities"].schema.metadata[b"description"]
    assert compiled.tables["graph_community_members"].schema.field("entity_id").metadata[b"description"]
    assert compiled.fingerprint == l5a_input_fingerprint(
        inputs["source"], **_publication_inputs(inputs, root), hierarchy_state=root,
    )


def test_absent_hierarchy_preserves_tables_definitions_and_fingerprint(hierarchy_input):
    inputs, _root, _outcome = hierarchy_input
    omitted = compile_l5a_publication(inputs["source"], **_publication_inputs(inputs))
    explicit_none = compile_l5a_publication(
        inputs["source"], **_publication_inputs(inputs), hierarchy_state=None,
    )
    assert omitted == explicit_none
    assert "graph_communities" not in omitted.tables
    assert all("derived_hierarchy" not in item for item in omitted.definitions.values())
    source = inputs["source"]
    expected = canonical_sha256({
        "stage": "schema2-structured-publication",
        "stage_contract_version": "1.0.0", "publication_code_version": "l5a-publication/1.2.0",
        "l4_receipt_hash": source.receipt.receipt_hash,
        "l4_output_manifest_hash": source.manifest.manifest_hash,
        "l3_artifact_manifest_id": source.input_manifest.artifact_manifest_id,
        "l3_artifact_manifest_hash": source.input_manifest.manifest_hash,
        "source_projection_hash": source.projection.projection_hash,
        "crosswalk_hashes": sorted(item.crosswalk_hash for item in inputs["crosswalks"]),
        "access_policy_hash": inputs["access_policy"].policy_hash,
        "governed_asset_hashes": sorted(item.asset_reference_hash for item in inputs["governed_assets"]),
        "target_ids": dict(sorted(inputs["target_ids"].items())),
    })
    assert omitted.fingerprint == expected


def test_parent_levels_and_ancestor_memberships_are_preserved(hierarchy_input, tmp_path):
    inputs, _root, _outcome = hierarchy_input
    source = inputs["source"]
    core_input = l5a_hierarchy._source_core_input(source)
    outcome = build_derived_community_hierarchy(
        list(core_input.entities), list(core_input.relationships),
        provider=_ScriptedNativeRows(), run_id="nested-run",
        config_id="nested-config", policy_id="nested-policy",
        seed=42, max_cluster_size=1,
    )
    root = tmp_path / "nested-hierarchy"
    write_derived_hierarchy_artifact(
        sealed=read_sealed_l4_hierarchy_rows(source), outcome=outcome,
        output_root=root, run_id="nested-run", config_id="nested-config",
        policy_id="nested-policy", seed=42, max_cluster_size=1,
        requested_provider_name=outcome.provider_id,
        requested_provider_version=outcome.provider_version,
        entity_evidence={
            row.entity_id: tuple(row.evidence_ids or ()) for row in core_input.entities
        },
        relationship_evidence={
            row.relationship_id: tuple(row.evidence_ids or ()) for row in core_input.relationships
        },
    )
    compiled = compile_l5a_publication(
        source, **_publication_inputs(inputs, root), hierarchy_state=root,
    )
    rows = compiled.tables["graph_communities"].to_pylist()
    parent = next(row for row in rows if not row["is_terminal"])
    assert parent["level"] == 1
    assert parent["native_depth"] == 0
    assert parent["size"] == len(core_input.entities)
    terminals = [row for row in rows if row["is_terminal"]]
    assert all(row["level"] == 0 and row["native_depth"] == 1 for row in terminals)
    assert all(row["parent_community_id"] == parent["community_id"] for row in terminals)
    members = compiled.tables["graph_community_members"].to_pylist()
    assert {row["entity_id"] for row in members if row["level"] == 1} == {
        row.entity_id for row in core_input.entities
    }


def test_fake_target_manifest_equivalence_and_receipt_include_hierarchy(hierarchy_input, tmp_path):
    inputs, root, _outcome = hierarchy_input
    kwargs = _publication_inputs(inputs, root)
    client = _FakeClient()
    result = run_l5a(
        inputs["source"], **kwargs, hierarchy_state=root,
        client=client, state_root=tmp_path / "l5a",
    )
    ids = {entry.artifact_id for entry in result.output_manifest.entries}
    assert {"l5a-table:graph_communities", "l5a-table:graph_community_members",
            "l5a-derived-table-crosswalk"} <= ids
    assert result.receipt.skip_key == result.compiled.fingerprint
    assert (result.run_root / "derived-table-crosswalk.json").is_file()
    require_l5a_publication_receipt(inputs["source"], result)
    second = run_l5a(
        inputs["source"], **kwargs, hierarchy_state=root,
        client=client, state_root=tmp_path / "l5a",
    )
    assert second.reused
    baseline = compile_l5a_publication(inputs["source"], **_publication_inputs(inputs))
    assert result.compiled.fingerprint != baseline.fingerprint
    assert result.projection_equivalences


@pytest.mark.parametrize("case,code", [
    ("source", "L5A_HIERARCHY_SOURCE_MISMATCH"),
    ("receipt", "L5A_HIERARCHY_INVALID_RECEIPT"),
    ("graph", "L5A_HIERARCHY_GRAPH_MISMATCH"),
    ("orphan", "L5A_HIERARCHY_INVALID_MEMBERSHIP"),
    ("partial", "L5A_HIERARCHY_INCOMPLETE_ARTIFACT"),
    ("bytes", "L5A_HIERARCHY_TABLE_INTEGRITY"),
    ("failed", "L5A_HIERARCHY_STATUS_NOT_PUBLISHABLE"),
    ("settings", "L5A_HIERARCHY_EXECUTION_MISMATCH"),
    ("provider", "L5A_HIERARCHY_PROVIDER_MISMATCH"),
    ("evidence", "L5A_HIERARCHY_EVIDENCE_MISMATCH"),
])
def test_invalid_artifact_fails_before_target_calls(hierarchy_input, tmp_path, case, code):
    inputs, root, _outcome = hierarchy_input
    if case == "source":
        _reseal(root, receipt_updates={"sealed_l4_receipt_id": "different-run"})
    elif case == "receipt":
        path = root / "derived-hierarchy-receipt.json"
        receipt = json.loads(path.read_text())
        receipt["seed"] = 99
        path.write_text(json.dumps(receipt))
    elif case == "graph":
        _reseal(root, receipt_updates={"input_graph_hash": "f" * 64})
    elif case == "orphan":
        _reseal(root, table_name="derived_community_memberships",
                mutate=lambda rows: rows[0].update(entity_id="entity:orphan"))
    elif case == "partial":
        (root / "derived-inter-community-relationship-diagnostics.parquet").unlink()
    elif case == "bytes":
        with (root / "derived-community-clusters.parquet").open("ab") as stream:
            stream.write(b"tampered")
    elif case == "failed":
        _reseal(root, receipt_updates={"status": "failed"})
    elif case == "settings":
        _reseal(root, receipt_updates={"max_cluster_size": 20})
    elif case == "provider":
        _reseal(root, receipt_updates={"realized_provider_version": "9.9.9"})
    else:
        _reseal(root, receipt_updates={"evidence_binding_hash": "e" * 64})
    client = _FakeClient()
    with pytest.raises(L5aPublicationError) as error:
        run_l5a(
            inputs["source"], **_publication_inputs(inputs),
            hierarchy_state=root, client=client, state_root=tmp_path / "l5a",
        )
    assert error.value.code == code
    assert not client.calls
    assert not (tmp_path / "l5a").exists()


def test_cli_materializes_human_readable_community_tables(hierarchy_input, tmp_path, monkeypatch):
    inputs, root, _outcome = hierarchy_input
    source = inputs["source"]
    from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource

    monkeypatch.setattr(SealedL4ServingSource, "from_run", lambda *args, **kwargs: source)
    result = CliRunner().invoke(app_cmd, [
        "publish-structured", "--l4-run", str(source.root),
        "--l3-root", str(tmp_path / "source" / ".fkg" / "l3"),
        "--workspace-id", WORKSPACE, "--plan", str(tmp_path / "plan.json"),
        "--hierarchy-state", str(root), "--materialize", str(tmp_path / "materialized"),
    ])
    assert result.exit_code == 0, result.output
    for name in ("graph_communities", "graph_community_members"):
        assert (tmp_path / "materialized" / "tables" / f"{name}.parquet").is_file()


def test_tmdl_retains_arrow_descriptions(hierarchy_input, tmp_path):
    from fabric_kg_builder.deploy.fabric_semantic_model_definition import (
        compile_fabric_semantic_model_definition,
    )

    inputs, root, _outcome = hierarchy_input
    tables, _binding = l5a_hierarchy.load_hierarchy_tables(inputs["source"], root)
    for name, table in tables.items():
        pq.write_table(table, tmp_path / f"{name}.parquet")
    compiled = compile_fabric_semantic_model_definition(
        tables_root=tmp_path, workspace_id=WORKSPACE, lakehouse_id=WORKSPACE,
        lakehouse="dbo",
    )
    import base64

    text = "\n".join(base64.b64decode(part["payload"]).decode() for part in compiled.parts)
    assert "/// Derived Leiden communities" in text
    assert "/// Canonical entity ID;" in text


def test_empty_unbound_has_a_distinct_code(hierarchy_input, monkeypatch):
    inputs, root, _outcome = hierarchy_input
    _reseal(root, receipt_updates={
        "status": "empty", "input_graph_hash": None, "execution_fingerprint": None,
    })
    monkeypatch.setattr(l5a_hierarchy, "_source_core_input", lambda _: SimpleNamespace(
        entities=(), relationships=(), input_graph_hash="0" * 64,
    ))
    with pytest.raises(L5aPublicationError, match="source graph had no entities/edges") as error:
        l5a_hierarchy.load_hierarchy_tables(inputs["source"], root)
    assert error.value.code == "L5A_HIERARCHY_EMPTY_UNBOUND"


def test_real_shared_helper_binds_receipt_input_graph_hash(hierarchy_input):
    """Unpatched production seam: build_hierarchy_core_rows(read_sealed_l4_hierarchy_rows(source))."""
    inputs, root, outcome = hierarchy_input
    receipt = json.loads((root / "derived-hierarchy-receipt.json").read_text())
    assert l5a_hierarchy._source_core_input is _PRODUCTION_CORE_INPUT
    converted = l5a_hierarchy._source_core_input(inputs["source"])
    direct = build_hierarchy_core_rows(read_sealed_l4_hierarchy_rows(inputs["source"]))
    assert converted.input_graph_hash == direct.input_graph_hash
    assert converted.input_graph_hash == receipt["input_graph_hash"] == outcome.input_graph_hash
    tables, _binding = l5a_hierarchy.load_hierarchy_tables(inputs["source"], root)
    members = tables["graph_community_members"].to_pylist()
    assert {row["entity_id"] for row in members} == {
        entity.entity_id for entity in converted.entities
    }


def test_prototype_upload_loop_receives_both_community_tables(hierarchy_input, tmp_path, monkeypatch):
    from fabric_kg_builder.deploy import schema2_prototype as prototype
    from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource

    inputs, root, _outcome = hierarchy_input
    source = inputs["source"]
    monkeypatch.setattr(SealedL4ServingSource, "from_run", lambda *args, **kwargs: source)
    compilation = prototype._compile(source.root, tmp_path, WORKSPACE, "hierarchy", hierarchy_state=root)
    uploaded = {}

    class FakeUploadRun:
        def __init__(self, *args):
            self.data = {"actions": {}}

        def items(self):
            return []

        def save(self):
            pass

        def create(self, kind, definition=None):
            if kind != "lakehouse":
                raise prototype.PrototypePublicationError("offline stop after Delta upload loop")
            self.data["actions"]["create:lakehouse"] = {
                "metadata": {"properties": {"defaultSchema": "dbo"}},
            }
            return WORKSPACE

        def delta(self, table_id, table, lakehouse_id):
            assert lakehouse_id == WORKSPACE
            uploaded[table_id] = table

    monkeypatch.setattr(prototype, "_Run", FakeUploadRun)
    kwargs = dict(
        l4_run=source.root, l3_root=tmp_path, workspace_id=WORKSPACE,
        name_prefix="hierarchy", plan_path=tmp_path / "prototype-plan.json",
        materialize_dir=tmp_path / "prototype-data", journal_path=tmp_path / "prototype-journal.json",
        hierarchy_state=root, approved_limitations=tuple(sorted(set(compilation.limitations))),
    )
    plan = prototype.publish_schema2_prototype(**kwargs, dry_run=True, approve_live=None)
    assert not plan["blockers"]
    with pytest.raises(prototype.PrototypePublicationError, match="offline stop after Delta upload loop"):
        prototype.publish_schema2_prototype(
            **kwargs, dry_run=False, approve_live=plan["plan_hash"],
        )
    assert set(uploaded) == set(compilation.tables)
    assert {"graph_communities", "graph_community_members"} <= set(uploaded)
    assert plan["provenance"]["derived_hierarchy"]["receipt_hash"]
