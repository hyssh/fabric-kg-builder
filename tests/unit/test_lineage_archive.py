"""Synthetic, authentic-stage P0 mechanics; not Surface or live Fabric proof."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from click.testing import CliRunner
import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.cli.lineage_cmd import lineage_cmd
from fabric_kg_builder.deploy.schema2_prototype import _compile, _materialize
from fabric_kg_builder.lineage.archive import (
    LineageArchiveError,
    create_archive,
    resolve_archive,
)
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from tests.unit.test_schema2_validation_stage import _l3, _pipeline

WORKSPACE = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def producer(tmp_path):
    root = tmp_path / "producer"
    root.mkdir()
    l1, domain, _ = _pipeline(root, "p0")
    l3 = _l3(root, l1, domain)
    l4 = run_l4(l3, state_root=root / "l4")
    compilation = _compile(l4.run_root, l3.state_root, WORKSPACE, "p0")
    _materialize(compilation, root / "publication")
    tables = [name for name in compilation.tables if name.startswith("l5a_type_")]
    table = tables[0]
    record = compilation.tables[table].to_pylist()[0]["__canonical_id"]
    return {
        "root": root, "l1_state": l1, "domain": domain, "l2_state": root / ".fkg/l2",
        "l3_root": l3.state_root, "l4_run": l4.run_root,
        "publication": root / "publication", "workspace_id": WORKSPACE,
        "name_prefix": "p0", "publication_version": "local-v1",
        "out": tmp_path / "archive", "source_root": root / "source",
        "table": table, "record": record, "compilation": compilation,
    }


def archive_inputs(producer):
    return {k: v for k, v in producer.items() if k not in {"root", "table", "record", "compilation"}}


def resolve_inputs(producer):
    return {
        "archive": producer["out"], "publication_version": producer["publication_version"],
        "table": producer["table"], "record": producer["record"],
    }


def test_archive_resolve_after_producer_removed_in_separate_consumer(producer, tmp_path):
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in producer["root"].rglob("*") if path.is_file()
    }
    created = create_archive(**archive_inputs(producer))
    for path, digest in before.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    result = resolve_archive(**resolve_inputs(producer), archive_hash=created["archive_hash"])
    assert result["metadata_status"] == "COMPLETE"
    assert result["original_source_retrieval_verified"] is True
    assert result["assertions"] and result["audit"] and result["candidates"]
    assert len(result["process_receipts"]) == 4
    assert result["process_coverage"] == "recorded_identities_and_receipts_only"
    assert result["live_fabric_verified"] is False
    producer["root"].rename(tmp_path / "producer-unavailable")
    command = [
        sys.executable, "-c",
        "import json,sys; from pathlib import Path; "
        "from fabric_kg_builder.lineage.archive import resolve_archive; "
        "r=resolve_archive(archive=Path(sys.argv[1]),publication_version=sys.argv[2],"
        "table=sys.argv[3],record=sys.argv[4]); print(json.dumps({"
        "'metadata':r['metadata_status'],'bytes':r['original_source_retrieval_verified']}))",
        str(producer["out"]), "local-v1", producer["table"], producer["record"],
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, cwd=tmp_path,
        env=os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"metadata": "COMPLETE", "bytes": True}


@pytest.mark.parametrize("change,status", [
    ("missing", "MISSING"), ("changed", "VERSION_MISMATCH"), ("denied", "DENIED"),
])
def test_source_access_status_does_not_replace_metadata(producer, monkeypatch, change, status):
    create_archive(**archive_inputs(producer))
    source = producer["source_root"] / "record.html"
    if change == "missing":
        source.unlink()
    elif change == "changed":
        source.write_bytes(b"not the recorded version")
    else:
        original_open = Path.open

        def guarded_open(self, *args, **kwargs):
            if self == source:
                raise PermissionError("controlled denied authorizer")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", guarded_open)
    result = resolve_archive(**resolve_inputs(producer), source_root=producer["source_root"])
    assert result["metadata_status"] == "COMPLETE"
    assert result["original_source_retrieval_verified"] is False
    assert {item["source_bytes_status"] for item in result["observations"]} == {status}


def test_no_source_read_without_explicit_root(producer, monkeypatch):
    options = archive_inputs(producer)
    options["source_root"] = None
    create_archive(**options)
    source = producer["source_root"] / "record.html"
    original_open = Path.open

    def forbid(self, *args, **kwargs):
        assert self != source, "implicit producer source access"
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid)
    result = resolve_archive(**resolve_inputs(producer))
    assert result["metadata_status"] == "COMPLETE"
    assert {item["source_bytes_status"] for item in result["observations"]} == {"NOT_ARCHIVED"}


def test_archived_original_missing_or_changed_is_not_verified(producer):
    create_archive(**archive_inputs(producer))
    source = next((producer["out"] / "sources").iterdir())
    source.write_bytes(b"changed")
    result = resolve_archive(**resolve_inputs(producer))
    assert result["observations"][0]["source_bytes_status"] == "VERSION_MISMATCH"
    source.unlink()
    result = resolve_archive(**resolve_inputs(producer))
    assert result["observations"][0]["source_bytes_status"] == "MISSING"


def test_historical_read_ignores_changed_current_schema(producer):
    create_archive(**archive_inputs(producer))
    producer["domain"].write_text("approval: {status: draft, notes: []}\n")
    result = resolve_archive(**resolve_inputs(producer))
    assert result["authority"]["schema_state"] == "frozen"
    assert result["metadata_status"] == "COMPLETE"


@pytest.mark.parametrize("change", ["tamper", "remove", "symlink"])
def test_metadata_artifact_integrity_fails_closed(producer, change):
    create_archive(**archive_inputs(producer))
    path = producer["out"] / "definitions" / "parquet.json"
    if change == "tamper":
        path.write_text("{}")
    elif change == "remove":
        path.unlink()
    else:
        target = producer["out"].parent / "outside.json"
        shutil.copyfile(path, target)
        path.unlink()
        path.symlink_to(target)
    with pytest.raises((LineageArchiveError, FileNotFoundError)):
        resolve_archive(**resolve_inputs(producer))


@pytest.mark.parametrize("overrides,error", [
    ({"publication_version": "unknown"}, "PUBLICATION_VERSION_MISMATCH"),
    ({"table": "unknown"}, "TABLE_NOT_FOUND"),
    ({"record": "unknown"}, "RECORD_NOT_FOUND"),
    ({"field": "unknown"}, "FIELD_NOT_FOUND"),
    ({"record": 4}, "INVALID_ID"),
    ({"table": ""}, "INVALID_ID"),
    ({"archive_hash": "0" * 64}, "ARCHIVE_HASH_MISMATCH"),
])
def test_explicit_lookup_errors(producer, overrides, error):
    create_archive(**archive_inputs(producer))
    with pytest.raises(LineageArchiveError, match=error):
        resolve_archive(**(resolve_inputs(producer) | overrides))


def test_derived_field_not_inferred_from_matching_values(producer):
    create_archive(**archive_inputs(producer))
    result = resolve_archive(**resolve_inputs(producer), field="__label")
    assert result["field_binding_status"] == "INCOMPLETE_DERIVED_OR_UNMAPPED_FIELD"
    assert result["resolution_status"] == "INCOMPLETE"


@pytest.mark.parametrize(("value", "value_type"), [(0, "integer"), (False, "boolean"), ("2026-07-01", "date")])
def test_authentic_single_property_field_contributors(tmp_path, monkeypatch, value, value_type):
    from tests.unit.test_schema2_validation_stage import _scalar_pipeline

    root = tmp_path / "producer"
    root.mkdir()
    l1, domain, _ = _scalar_pipeline(root, monkeypatch, value=value, value_type=value_type)
    l3 = _l3(root, l1, domain)
    l4 = run_l4(l3, state_root=root / "l4")
    compilation = _compile(l4.run_root, l3.state_root, WORKSPACE, "p0")
    _materialize(compilation, root / "publication")
    definition = compilation.definitions["parquet"]
    semantic_type = next(item for item in definition["semantic_types"] if item["properties"])
    table = semantic_type["physical_table_id"]
    field = semantic_type["properties"][0]["physical_column_id"]
    record = compilation.tables[table].to_pylist()[0]["__canonical_id"]
    archive = tmp_path / "archive"
    create_archive(
        l1_state=l1, domain=domain, l2_state=root / ".fkg/l2", l3_root=l3.state_root,
        l4_run=l4.run_root, publication=root / "publication", workspace_id=WORKSPACE,
        name_prefix="p0", publication_version="local-v1", out=archive, source_root=root / "source",
    )
    root.rename(tmp_path / "unavailable")
    result = resolve_archive(
        archive=archive, publication_version="local-v1", table=table, record=record, field=field,
    )
    assert result["field_binding_status"] == "COMPLETE"
    assert len(result["assertions"]) == 1
    assert result["assertions"][0]["semantic_property_id"] == "property:records.reading"
    assert result["field_transform"]["extraction_observations"]
    assert result["candidates"] and result["original_source_retrieval_verified"]


def test_audit_lookup_never_promotes_to_serving(producer):
    create_archive(**archive_inputs(producer))
    index = json.loads((producer["out"] / "index.json").read_text())
    record = index["audit"][0]["candidate_id"]
    result = resolve_archive(
        archive=producer["out"], publication_version="local-v1", table="audit_candidates", record=record,
    )
    assert result["resolution_status"] == "AUDIT_ONLY"
    assert result["assertions"] == []
    assert result["observations"]
    assert result["observations"][0]["verified_assertion_evidence"] is False


def test_nonasserted_candidate_retains_audit_only_source(tmp_path):
    root = tmp_path / "producer"
    root.mkdir()

    def reject_subject(candidates, _):
        candidates[1]["anchors"][0]["quote"] = "not in the source"
        return candidates

    l1, domain, _ = _pipeline(root, "p0", mutate=reject_subject)
    l3 = _l3(root, l1, domain)
    l4 = run_l4(l3, state_root=root / "l4")
    compilation = _compile(l4.run_root, l3.state_root, WORKSPACE, "p0")
    _materialize(compilation, root / "publication")
    archive = tmp_path / "archive"
    create_archive(
        l1_state=l1, domain=domain, l2_state=root / ".fkg/l2", l3_root=l3.state_root,
        l4_run=l4.run_root, publication=root / "publication", workspace_id=WORKSPACE,
        name_prefix="p0", publication_version="local-v1", out=archive, source_root=root / "source",
    )
    index = json.loads((archive / "index.json").read_text())
    rejected = next(row for row in index["audit"] if row["lifecycle_state"] != "asserted")
    result = resolve_archive(
        archive=archive, publication_version="local-v1", table="audit_candidates",
        record=rejected["candidate_id"],
    )
    assert result["resolution_status"] == "AUDIT_ONLY"
    assert result["assertions"] == []
    assert result["observations"] and result["original_source_retrieval_verified"]


def test_multiple_property_assertions_are_retained_but_not_guessed():
    import pyarrow as pa
    from fabric_kg_builder.lineage.archive import _assertions

    property_rows = [{
        "property_assertion_id": f"p{index}", "entity_id": "e1", "semantic_property_id": "reading",
        "normalized_value_json": value, "value_type": "string", "evidence_span_ids": [f"ev{index}"],
    } for index, value in enumerate(['"READING"', '"Reading"'])]
    tables = {
        "typed": pa.table({"__canonical_id": ["e1"], "reading": ["READING"]}),
        "l4_semantic_asserted_entities": pa.table({"entity_id": ["e1"]}),
        "l4_semantic_asserted_properties": pa.Table.from_pylist(property_rows),
        "l4_semantic_asserted_relationships": pa.table({"relationship_id": []}),
    }
    files = {}
    for name, table in tables.items():
        buffer = pa.BufferOutputStream()
        pq.write_table(table, buffer)
        files[f"tables/{name}.parquet"] = buffer.getvalue().to_pybytes()
    files["definitions/parquet.json"] = json.dumps({
        "semantic_types": [{"physical_table_id": "typed", "properties": [{
            "physical_column_id": "reading", "canonical_property_id": "reading",
        }]}],
    }).encode()
    _, assertions, status, transform = _assertions(
        {"tables": list(tables)}, files, "typed", "e1", "reading",
    )
    assert status == "INCOMPLETE_PROPERTY_CONTRIBUTORS"
    assert assertions == property_rows
    assert transform["contributor_selection"] == "unknown"


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b"{", b"\xff"])
def test_strict_archive_json(raw):
    from fabric_kg_builder.lineage.archive import _json

    with pytest.raises(LineageArchiveError, match="INVALID_JSON"):
        _json(raw)


def test_json_size_bound_is_explicit(monkeypatch):
    import fabric_kg_builder.lineage.archive as module

    monkeypatch.setattr(module, "MAX_JSON_BYTES", 4)
    with pytest.raises(LineageArchiveError, match="JSON_TOO_LARGE"):
        module._json(b'"12345"')


def test_publication_value_drift_rejected(producer):
    table = producer["publication"] / "tables" / f"{producer['table']}.parquet"
    value = pq.read_table(table)
    pq.write_table(value.slice(0, 0), table)
    with pytest.raises(LineageArchiveError, match="PUBLICATION_MISMATCH"):
        create_archive(**archive_inputs(producer))
    assert not producer["out"].exists()


def test_source_change_prevents_archival(producer):
    (producer["source_root"] / "record.html").write_bytes(b"changed")
    with pytest.raises(LineageArchiveError, match="SOURCE_VERSION_MISMATCH"):
        create_archive(**archive_inputs(producer))
    assert not producer["out"].exists()


def test_missing_l3_evidence_is_not_metadata_only_success(producer):
    evidence_file = next(producer["l3_root"].rglob("evidence-spans/*.json"))
    evidence_file.unlink()
    with pytest.raises(FileNotFoundError):
        create_archive(**archive_inputs(producer))
    assert not producer["out"].exists()


def test_create_only_and_disjoint_output(producer):
    options = archive_inputs(producer)
    with pytest.raises(RuntimeError, match="COLLISION"):
        create_archive(**(options | {"out": producer["l4_run"] / "archive"}))
    create_archive(**options)
    with pytest.raises(RuntimeError, match="ALREADY_EXISTS"):
        create_archive(**options)
    alias = producer["out"].parent / "alias"
    alias.symlink_to(producer["l4_run"], target_is_directory=True)
    with pytest.raises(RuntimeError, match="COLLISION"):
        create_archive(**(options | {"out": alias / "new"}))


def test_cli_archive_resolve_and_unknown_field(producer):
    runner = CliRunner()
    args = ["archive"]
    for name, value in archive_inputs(producer).items():
        args.extend([f"--{name.replace('_', '-')}", str(value)])
    result = runner.invoke(lineage_cmd, args)
    assert result.exit_code == 0, result.output
    result = runner.invoke(lineage_cmd, [
        "resolve", "--archive", str(producer["out"]), "--publication-version", "local-v1",
        "--table", producer["table"], "--record", producer["record"], "--field", "unknown",
    ])
    assert result.exit_code != 0
    assert "FIELD_NOT_FOUND" in result.output


def test_cli_preserves_json_stdout_and_surfaces_backend_diagnostics(tmp_path, monkeypatch, capsys):
    import fabric_kg_builder.lineage.archive as archive

    def noisy_backend(**kwargs):
        print("dependency diagnostic")
        return {"metadata_status": "INCOMPLETE"}

    monkeypatch.setattr(archive, "resolve_archive", noisy_backend)
    lineage_cmd.main([
        "resolve", "--archive", str(tmp_path), "--publication-version", "local-v1",
        "--table", "table", "--record", "record",
    ], standalone_mode=False)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"metadata_status": "INCOMPLETE"}
    assert "dependency diagnostic" in captured.err
