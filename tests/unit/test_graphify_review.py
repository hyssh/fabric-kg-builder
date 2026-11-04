"""Project-boundary checks, not Graphify execution or quality acceptance."""

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.graphify_review_cmd import review_graphify_import_cmd
from fabric_kg_builder.cli.main import cli
from fabric_kg_builder.semantic.graphify_review import (
    GRAPHIFY_VERSION,
    UPSTREAM_SHA,
    read_review_json,
    review_graphify_import,
)
from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import (
    build_hierarchy_core_rows,
    load_sealed_l4_hierarchy_rows,
)
from tests.unit.test_sealed_l4_hierarchy_source import _run


@pytest.fixture
def inputs(tmp_path):
    l3, l4 = _run(tmp_path)
    sealed = load_sealed_l4_hierarchy_rows(
        l4.run_root, input_manifest_search_roots=(l3.state_root,)
    )
    core = build_hierarchy_core_rows(sealed)
    external = {row.entity_id: f"external-{index}"
                for index, row in enumerate(core.entities)}
    graph = {
        "directed": True,
        "multigraph": False,
        "graph": {"schema_version": 1, "graphify_version": GRAPHIFY_VERSION},
        "nodes": [
            {"id": external[row.entity_id], "community": 0,
             "label": "untrusted label", "file_type": "image",
             "source_file": "/must/not/be/opened"}
            for row in core.entities
        ],
        "links": [
            {"source": external[row.source_entity_id],
             "target": external[row.target_entity_id],
             "relation": row.relationship_type, "confidence": "INFERRED"}
            for row in core.relationships
        ],
    }
    bindings = {
        "schema_version": "1.0", "upstream_sha": UPSTREAM_SHA,
        "graphify_version": GRAPHIFY_VERSION,
        "artifact_sha256": "", "sealed_manifest_hash": sealed.manifest.manifest_hash,
        "entities": [
            {"graphify_id": external[row.entity_id], "entity_id": row.entity_id,
             "semantic_type_id": row.entity_type, "evidence_span_ids": row.evidence_ids}
            for row in core.entities
        ],
        "relationships": [
            {"link_index": index, "relationship_id": row.relationship_id,
             "semantic_relationship_id": row.relationship_type,
             "evidence_span_ids": row.evidence_ids}
            for index, row in enumerate(core.relationships)
        ],
    }
    return tmp_path, l3, l4, sealed, graph, bindings


def _write(inputs):
    tmp_path, _, _, _, graph, bindings = inputs
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    bindings["artifact_sha256"] = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    bindings_path = tmp_path / "bindings.json"
    bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
    return graph_path, bindings_path


def _review(inputs):
    graph_path, bindings_path = _write(inputs)
    return review_graphify_import(
        sealed=inputs[3], graph_path=graph_path, bindings_path=bindings_path
    )


def _invoke(inputs, output, *extra):
    graph_path, bindings_path = _write(inputs)
    return CliRunner().invoke(
        review_graphify_import_cmd,
        ["--source", str(inputs[2].run_root),
         "--input-manifest-search-root", str(inputs[1].state_root),
         "--graph", str(graph_path), "--bindings", str(bindings_path),
         "--out", str(output), *extra],
    )


def test_review_is_flat_non_authoritative_and_input_bytes_unchanged(inputs):
    graph_path, bindings_path = _write(inputs)
    before = {path: path.read_bytes() for path in inputs[2].run_root.rglob("*")
              if path.is_file()}
    raw_graph, raw_bindings = graph_path.read_bytes(), bindings_path.read_bytes()
    report = review_graphify_import(
        sealed=inputs[3], graph_path=graph_path, bindings_path=bindings_path
    )
    assert report["status"] == "shadow_review_only"
    assert report["publishable"] is False
    assert report["producer_provenance_verified"] is False
    assert report["flat_communities"][0]["entity_ids"] == sorted(
        row["entity_id"] for row in inputs[3].entities
    )
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert graph_path.read_bytes() == raw_graph
    assert bindings_path.read_bytes() == raw_bindings
    assert report == review_graphify_import(
        sealed=inputs[3], graph_path=graph_path, bindings_path=bindings_path
    )


@pytest.mark.parametrize("mutation,match", [
    (lambda g, b: g["nodes"].append(dict(g["nodes"][0])), "duplicate node"),
    (lambda g, b: g["nodes"][0].update(id=7), "invalid node id"),
    (lambda g, b: g["nodes"][0].update(id=""), "invalid node id"),
    (lambda g, b: g["nodes"][0].update(id=True), "invalid node id"),
    (lambda g, b: g["nodes"][0].update(community=None), "community required"),
    (lambda g, b: g["nodes"][0].update(community=True), "community required"),
    (lambda g, b: g.update(directed=False), "directed"),
    (lambda g, b: g["links"][0].update(source="missing"), "endpoint"),
    (lambda g, b: g["links"][0].update(source=[]), "endpoint"),
    (lambda g, b: g["links"][0].update(relation="invented"), "type differs"),
    (lambda g, b: g["links"].clear(), "coverage"),
    (lambda g, b: b["entities"][0].update(evidence_span_ids=[]), "evidence"),
    (lambda g, b: b["entities"][0].update(evidence_span_ids=["fake"]), "evidence"),
    (lambda g, b: b["entities"][0].update(semantic_type_id="fake"), "type binding"),
    (lambda g, b: b["entities"].append(dict(b["entities"][0])), "bijection"),
    (lambda g, b: b["relationships"][0].update(relationship_id="fake"), "coverage"),
    (lambda g, b: b.update(sealed_manifest_hash="0" * 64), "manifest hash"),
    (lambda g, b: b.update(upstream_sha="0" * 40), "upstream_sha"),
    (lambda g, b: b.update(unknown="not allowed"), "Extra inputs"),
    (lambda g, b: g.update(hyperedges=[{}]), "hyperedges"),
    (lambda g, b: g.update(nodes="not an array"), "array"),
    (lambda g, b: g["graph"].update(graphify_version="unverified"), "version conflicts"),
    (lambda g, b: g["graph"].update(schema_version=True), "schema_version"),
    (lambda g, b: g["graph"].update(schema_version="1"), "schema_version"),
    (lambda g, b: g.update(edges=[]), "uses links"),
    (lambda g, b: g["links"].append(dict(g["links"][0])), "coverage"),
    (lambda g, b: b["relationships"].append(dict(b["relationships"][0])), "bijection"),
    (lambda g, b: b["relationships"][0].update(evidence_span_ids=[]), "evidence"),
    (lambda g, b: b["relationships"][0].update(semantic_relationship_id="fake"), "type differs"),
])
def test_rejects_mutated_candidates(inputs, mutation, match):
    mutation(inputs[4], inputs[5])
    with pytest.raises(ValueError, match=match):
        _review(inputs)


def test_rejects_reversed_relationship(inputs):
    link = inputs[4]["links"][0]
    link["source"], link["target"] = link["target"], link["source"]
    with pytest.raises(ValueError, match="directed endpoints"):
        _review(inputs)


def test_rejects_tampered_graph_bytes(inputs):
    graph_path, bindings_path = _write(inputs)
    graph_path.write_text(graph_path.read_text() + " ")
    with pytest.raises(ValueError, match="SHA256"):
        review_graphify_import(
            sealed=inputs[3], graph_path=graph_path, bindings_path=bindings_path
        )


def test_cli_is_registered_and_writes_create_only_report(inputs):
    output = inputs[0] / "reviews" / "report.json"
    result = _invoke(inputs, output)
    assert result.exit_code == 0, result.output
    assert "not publishable" in result.output
    report = json.loads(output.read_text())
    assert report["publishable"] is False
    original = output.read_bytes()
    assert _invoke(inputs, output).exit_code == 1
    assert output.read_bytes() == original
    assert "review-graphify-import" in CliRunner().invoke(cli, ["--help"]).output


def test_review_cannot_be_loaded_for_hierarchy_publication(inputs):
    from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
    from fabric_kg_builder.serving.l5a_hierarchy import load_hierarchy_tables
    from fabric_kg_builder.serving.structured_publication import L5aPublicationError

    output = inputs[0] / "reviews" / "derived-hierarchy-receipt.json"
    assert _invoke(inputs, output).exit_code == 0
    source = SealedL4ServingSource.from_run(
        inputs[2].run_root, input_manifest_search_roots=(inputs[1].state_root,)
    )
    with pytest.raises(L5aPublicationError, match="L5A_HIERARCHY_INVALID_RECEIPT"):
        load_hierarchy_tables(source, output.parent)


@pytest.mark.parametrize("relative_root", [
    ".fkg/l1", ".fkg/l2", ".fkg/l3", ".fkg/l4",
    ".fkg/derived-hierarchy", "build/release",
])
def test_cli_protects_default_state(inputs, monkeypatch, relative_root):
    monkeypatch.chdir(inputs[0])
    output = inputs[0] / relative_root / "review.json"
    result = _invoke(inputs, output)
    assert result.exit_code == 1, result.output
    assert "COLLISION" in result.output
    assert not output.exists()


@pytest.mark.parametrize("bad_input", ["graph", "bindings", "size"])
def test_cli_reports_input_format_failures(inputs, monkeypatch, bad_input):
    graph_path, bindings_path = _write(inputs)
    if bad_input == "graph":
        graph_path.write_text("{broken")
    elif bad_input == "bindings":
        bindings_path.write_text("[]")
    else:
        monkeypatch.setattr(
            "fabric_kg_builder.semantic.graphify_review.MAX_REVIEW_INPUT_BYTES", 8
        )
    output = inputs[0] / "review.json"
    result = CliRunner().invoke(
        review_graphify_import_cmd,
        ["--source", str(inputs[2].run_root),
         "--input-manifest-search-root", str(inputs[1].state_root),
         "--graph", str(graph_path), "--bindings", str(bindings_path),
         "--out", str(output)],
    )
    assert result.exit_code == 1
    assert "Graphify shadow review rejected" in result.output
    assert not output.exists()


@pytest.mark.parametrize("root_index", [1, 2])
def test_cli_protects_real_authority_roots(inputs, root_index):
    root = inputs[root_index]
    output = (root.state_root if root_index == 1 else root.run_root) / "review.json"
    result = _invoke(inputs, output)
    assert result.exit_code == 1, result.output
    assert "COLLISION" in result.output
    assert not output.exists()


def test_cli_protects_symlink_and_custom_state(inputs):
    alias = inputs[0] / "alias"
    alias.symlink_to(inputs[2].run_root, target_is_directory=True)
    assert _invoke(inputs, alias / "review.json").exit_code == 1
    custom = inputs[0] / "custom-l1"
    assert _invoke(inputs, custom / "review.json", "--protected-root", str(custom)).exit_code == 1
    dangling = inputs[0] / "dangling.json"
    dangling.symlink_to(inputs[0] / "missing.json")
    assert _invoke(inputs, dangling).exit_code == 1
    assert not (inputs[0] / "missing.json").exists()


def test_invalid_candidate_leaves_no_output(inputs):
    inputs[4]["nodes"][0]["id"] = None
    output = inputs[0] / "reviews" / "report.json"
    result = _invoke(inputs, output)
    assert result.exit_code == 1
    assert "invalid node id" in result.output
    assert not output.parent.exists()


@pytest.mark.parametrize("raw,match", [
    ('{"nodes":[],"nodes":[]}', "duplicate JSON key"),
    ('{"value":NaN}', "non-finite"),
    ('{"value":1e9999}', "non-finite"),
    ('{broken', "Expecting property"),
])
def test_json_errors_are_explicit(tmp_path, raw, match):
    path = tmp_path / "bad.json"
    path.write_text(raw)
    with pytest.raises(ValueError, match=match):
        read_review_json(path)


def test_json_recursion_error_is_explicit(tmp_path, monkeypatch):
    def too_deep(*args, **kwargs):
        raise RecursionError("decoder recursion limit")

    monkeypatch.setattr("fabric_kg_builder.semantic.graphify_review.json.loads", too_deep)
    path = tmp_path / "nested.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="deeply nested"):
        read_review_json(path)


def test_oversize_json_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "fabric_kg_builder.semantic.graphify_review.MAX_REVIEW_INPUT_BYTES", 8
    )
    path = tmp_path / "large.json"
    path.write_text('{"nodes":[]}')
    with pytest.raises(ValueError, match="byte limit"):
        read_review_json(path)
