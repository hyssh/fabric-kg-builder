"""Actual local archive + actual dry-run plan, synthetic deployed journal/readback.

No Fabric call or claim that a fabricated journal is a service attestation.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.lineage_cmd import lineage_cmd
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.deploy.schema2_prototype import (
    FORMAT_VERSION, STRUCTURAL_PUBLICATION, _table_proof, publish_schema2_prototype,
)
from fabric_kg_builder.lineage.archive import LineageArchiveError, create_archive
from fabric_kg_builder.lineage.publication_binding import (
    DeltaReadback, OneLakeDeltaReader, _delta_path, bind_publication, resolve_stored_record,
)
from tests.unit.test_lineage_archive import WORKSPACE, archive_inputs, producer  # noqa: F401

LAKEHOUSE = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
def deployed(producer, tmp_path):
    return deployment_fixture(producer, tmp_path)


def deployment_fixture(producer, tmp_path):
    p = producer
    plan_path, journal_path = p["root"] / "prototype-plan.json", p["root"] / "prototype-journal.json"
    plan = publish_schema2_prototype(
        l4_run=p["l4_run"], l3_root=p["l3_root"], workspace_id=WORKSPACE,
        name_prefix="p0", dry_run=True, approve_live=None, plan_path=plan_path,
        journal_path=journal_path, materialize_dir=p["publication"],
        approved_limitations=tuple(p["compilation"].limitations),
    )
    assert not plan["blockers"]
    journal = {
        "journal_version": FORMAT_VERSION, "plan_hash": plan["plan_hash"],
        "run_id": plan["run_id"], "workspace_id": WORKSPACE,
        "policy": "create-only-retain-partial", "status": "structural-verification-complete",
        "structural_publication": STRUCTURAL_PUBLICATION,
        "ontology_readiness": "companion-bindings-scalars-and-endpoint-pairs-verified",
        "item_ids": {"lakehouse": LAKEHOUSE},
        "actions": {
            "create:lakehouse": {
                "status": "identity-verified", "item_id": LAKEHOUSE, "http_status": 201,
                "metadata": {
                    "id": LAKEHOUSE, "type": "Lakehouse", "displayName": plan["names"]["lakehouse"],
                    "properties": {"defaultSchema": "dbo"},
                },
            },
            **{
                f"delta:{table}": {
                    "status": "verified", "delta_version": 0,
                    "path": _delta_path(WORKSPACE, LAKEHOUSE, table),
                    "table_proof": proof, "readback": proof,
                } for table, proof in plan["tables"].items()
            },
        },
    }
    journal_path.write_text(json.dumps(journal))
    archive = create_archive(**archive_inputs(p))
    binding_root = tmp_path / "publication-binding"
    return {
        "producer": p, "archive": p["out"], "archive_hash": archive["archive_hash"],
        "plan": plan_path, "journal": journal_path, "plan_value": plan, "journal_value": journal,
        "out": binding_root,
    }


def _bind(deployed):
    return bind_publication(**{
        key: deployed[key] for key in ("archive", "archive_hash", "plan", "journal", "out")
    })


def _resolve(deployed, binding):
    return {
        "binding": deployed["out"], "binding_hash": binding["binding_hash"], "archive": deployed["archive"],
        "workspace_id": WORKSPACE, "lakehouse_id": LAKEHOUSE, "table": deployed["producer"]["table"],
        "delta_version": 0, "record": deployed["producer"]["record"],
    }


class FakeReader:
    def __init__(self, deployed):
        self.calls = []
        p = deployed["producer"]
        self.table = p["compilation"].tables[p["table"]]
        self.commit = {
            "version": 0, "prototype_run_id": deployed["plan_value"]["run_id"],
            "prototype_plan_hash": deployed["plan_value"]["plan_hash"],
            "prototype_action": f"delta:{p['table']}",
        }
        self.version = 0

    def read(self, path, *, version, max_rows):
        self.calls.append((path, version, max_rows))
        return DeltaReadback(self.table, self.version, self.commit)


def test_offline_binding_does_not_claim_remote_read(deployed, monkeypatch):
    def forbid(*args, **kwargs):
        raise AssertionError("unapproved remote read")

    monkeypatch.setattr(OneLakeDeltaReader, "read", forbid)
    binding = _bind(deployed)
    result = resolve_stored_record(**_resolve(deployed, binding))
    assert result["stored_readback_status"] == "NOT_RUN"
    assert result["stored_record_verified"] is False
    assert result["live_fabric_verified"] is False
    assert result["metadata_status"] == "COMPLETE"
    assert result["stored_publication"]["delta_version"] == 0
    assert binding["remote_readback_performed_by_binding"] is False


def test_exact_readback_then_historical_lookup_with_producer_unavailable(deployed, tmp_path):
    binding = _bind(deployed)
    fake = FakeReader(deployed)
    relocated = tmp_path / "consumer"
    shutil.copytree(deployed["out"], relocated / "binding")
    shutil.copytree(deployed["archive"], relocated / "archive")
    deployed["producer"]["root"].rename(tmp_path / "unavailable-producer")
    deployed["out"].rename(tmp_path / "unavailable-binding")
    deployed["archive"].rename(tmp_path / "unavailable-archive")
    result = resolve_stored_record(**(
        _resolve(deployed, binding) | {"binding": relocated / "binding", "archive": relocated / "archive"}
    ), read_remote=True, approve_read=binding["binding_hash"], reader=fake)
    assert fake.calls == [(_delta_path(WORKSPACE, LAKEHOUSE, deployed["producer"]["table"]), 0, 10_000)]
    assert result["stored_record_verified"] is True
    assert result["stored_readback_status"] == "VERIFIED"
    assert result["live_fabric_verified"] is False  # Fake is not Fabric.
    assert result["metadata_status"] == "COMPLETE"
    assert result["original_source_retrieval_verified"]


@pytest.mark.parametrize("change", ["status", "workspace", "plan_hash", "lakehouse", "readback", "version", "bool_version", "bool_rows", "path"])
def test_journal_identity_and_readback_mismatch_rejected(deployed, change):
    journal = copy.deepcopy(deployed["journal_value"])
    table = deployed["producer"]["table"]
    action = journal["actions"][f"delta:{table}"]
    if change == "status":
        journal["status"] = "partial-retained"
    elif change == "workspace":
        journal["workspace_id"] = LAKEHOUSE
    elif change == "plan_hash":
        journal["plan_hash"] = "0" * 64
    elif change == "lakehouse":
        journal["item_ids"]["lakehouse"] = WORKSPACE
    elif change == "readback":
        action["readback"]["values_hash"] = "0" * 64
    elif change == "version":
        action["delta_version"] = 1
    elif change == "bool_version":
        action["delta_version"] = False
    elif change == "bool_rows":
        action["readback"]["row_count"] = True
    else:
        action["path"] = "https://untrusted.example/should-never-open"
    deployed["journal"].write_text(json.dumps(journal))
    with pytest.raises((LineageArchiveError, ValueError)):
        _bind(deployed)
    assert not deployed["out"].exists()


@pytest.mark.parametrize("overrides", [
    {"workspace_id": LAKEHOUSE}, {"lakehouse_id": WORKSPACE},
    {"delta_version": 1}, {"delta_version": False},
    {"table": "../outside"}, {"binding_hash": "0" * 64},
])
def test_mismatched_target_never_calls_reader(deployed, overrides):
    binding = _bind(deployed)
    fake = FakeReader(deployed)
    with pytest.raises(LineageArchiveError):
        resolve_stored_record(**(_resolve(deployed, binding) | overrides),
                              read_remote=True, approve_read=binding["binding_hash"], reader=fake)
    assert fake.calls == []


def test_missing_approval_and_budget_do_not_call_reader(deployed):
    binding = _bind(deployed)
    fake = FakeReader(deployed)
    with pytest.raises(LineageArchiveError, match="EXACT_READ_APPROVAL"):
        resolve_stored_record(**_resolve(deployed, binding), read_remote=True, reader=fake)
    fake.table = pa.concat_tables([fake.table, fake.table])
    with pytest.raises(LineageArchiveError, match="READBACK_ROW_CAP_EXCEEDED"):
        p = _resolve(deployed, binding)
        p["table"] = "l4_semantic_asserted_entities"  # Two rows, cap one.
        p["record"] = deployed["producer"]["record"]
        resolve_stored_record(**p, read_remote=True, approve_read=binding["binding_hash"], max_rows=1, reader=fake)
    assert not fake.calls


@pytest.mark.parametrize("change", ["wrong_run", "wrong_action", "wrong_version", "values", "row_count"])
def test_remote_commit_and_values_fail_without_success_fallback(deployed, change):
    binding = _bind(deployed)
    fake = FakeReader(deployed)
    if change == "wrong_run":
        fake.commit["prototype_run_id"] = "not-this-run"
    elif change == "wrong_action":
        fake.commit["prototype_action"] = "delta:other"
    elif change == "wrong_version":
        fake.version = 1
    elif change == "row_count":
        fake.table = fake.table.slice(0, 0)
    else:
        rows = fake.table.to_pylist()
        rows[0]["__label"] = "changed"
        fake.table = pa.Table.from_pylist(rows, schema=fake.table.schema)
    with pytest.raises(LineageArchiveError, match="MISMATCH"):
        resolve_stored_record(**_resolve(deployed, binding), read_remote=True,
                              approve_read=binding["binding_hash"], reader=fake)
    assert len(fake.calls) == 1


def test_backend_error_does_not_expose_secrets_or_fallback(deployed):
    binding = _bind(deployed)

    class Denied:
        def read(self, *args, **kwargs):
            raise PermissionError("bearer_token=secret-should-never-appear")

    with pytest.raises(LineageArchiveError, match="STORED_READ_FAILED: PermissionError") as error:
        resolve_stored_record(**_resolve(deployed, binding), read_remote=True,
                              approve_read=binding["binding_hash"], reader=Denied())
    assert "secret-should-never-appear" not in str(error.value)


@pytest.mark.parametrize("change", ["tamper_journal", "missing_plan", "symlink_plan", "archive_mismatch"])
def test_binding_evidence_and_archive_integrity(deployed, change, tmp_path):
    binding = _bind(deployed)
    path = deployed["out"] / "plan.json"
    if change == "tamper_journal":
        (deployed["out"] / "journal.json").write_text("{}")
    elif change == "missing_plan":
        path.unlink()
    elif change == "symlink_plan":
        raw = path.read_bytes()
        path.unlink()
        outside = tmp_path / "outside.json"
        outside.write_bytes(raw)
        path.symlink_to(outside)
    else:
        (deployed["archive"] / "index.json").write_text("{}")
    with pytest.raises((LineageArchiveError, OSError)):
        resolve_stored_record(**_resolve(deployed, binding))


def test_binding_destination_create_only_and_archive_disjoint(deployed):
    with pytest.raises(RuntimeError, match="COLLISION"):
        bind_publication(**{
            key: value for key, value in deployed.items()
            if key in {"archive", "archive_hash", "plan", "journal"}
        }, out=deployed["archive"] / "binding")
    _bind(deployed)
    with pytest.raises(RuntimeError, match="ALREADY_EXISTS"):
        _bind(deployed)


def test_cli_binds_and_resolves_without_remote_opt_in(deployed):
    runner = CliRunner()
    args = ["bind-publication"]
    for key in ("archive", "archive_hash", "plan", "journal", "out"):
        args += [f"--{key.replace('_', '-')}", str(deployed[key])]
    result = runner.invoke(lineage_cmd, args)
    assert result.exit_code == 0, result.output
    binding = json.loads(result.stdout)
    args = ["resolve-stored"]
    for key, value in _resolve(deployed, binding).items():
        args += [f"--{key.replace('_', '-')}", str(value)]
    result = runner.invoke(lineage_cmd, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["stored_readback_status"] == "NOT_RUN"


def test_real_delta_reader_pins_version_and_returns_ownership(tmp_path, monkeypatch):
    """Local Delta backend exercise, never OneLake/Fabric."""
    import azure.identity
    import deltalake

    table = pa.table({"__canonical_id": ["e1"], "__label": ["original"]})
    path = tmp_path / "delta"
    deltalake.write_deltalake(path, table, commit_properties=deltalake.CommitProperties(custom_metadata={
        "prototype_run_id": "run:test", "prototype_plan_hash": "f" * 64, "prototype_action": "delta:t",
    }))
    deltalake.write_deltalake(path, pa.table({"__canonical_id": ["e1"], "__label": ["new"]}), mode="overwrite")
    calls = []
    real_delta = deltalake.DeltaTable

    def local_delta(uri, **kwargs):
        calls.append((uri, kwargs.get("version"), kwargs.get("without_files", False)))
        return real_delta(path, **{key: value for key, value in kwargs.items() if key != "storage_options"})

    class Credential:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_token(self, scope):
            assert scope == "https://storage.azure.com/.default"
            return type("Token", (), {"token": "fake-offline-token"})()

    monkeypatch.setattr(azure.identity, "AzureCliCredential", Credential)
    monkeypatch.setattr(deltalake, "DeltaTable", local_delta)
    observed = OneLakeDeltaReader().read(_delta_path(WORKSPACE, LAKEHOUSE, "t"), version=0, max_rows=5)
    assert calls == [
        (_delta_path(WORKSPACE, LAKEHOUSE, "t"), None, True),
        (_delta_path(WORKSPACE, LAKEHOUSE, "t"), 0, False),
    ]
    assert observed.delta_version == 0
    assert observed.commit["prototype_run_id"] == "run:test"
    assert _table_proof(observed.table) == _table_proof(table)


def test_actual_single_property_field_through_stored_identity(tmp_path, monkeypatch):
    from fabric_kg_builder.deploy.schema2_prototype import _compile, _materialize
    from fabric_kg_builder.serving.lifecycle_projection import run_l4
    from tests.unit.test_schema2_validation_stage import _l3, _scalar_pipeline

    root = tmp_path / "producer"
    root.mkdir()
    l1, domain, _ = _scalar_pipeline(root, monkeypatch, value=0, value_type="integer")
    l3 = _l3(root, l1, domain)
    l4 = run_l4(l3, state_root=root / "l4")
    compilation = _compile(l4.run_root, l3.state_root, WORKSPACE, "p0")
    _materialize(compilation, root / "publication")
    semantic_type = next(item for item in compilation.definitions["parquet"]["semantic_types"] if item["properties"])
    table = semantic_type["physical_table_id"]
    field = semantic_type["properties"][0]["physical_column_id"]
    record = compilation.tables[table].to_pylist()[0]["__canonical_id"]
    p = {
        "root": root, "l1_state": l1, "domain": domain, "l2_state": root / ".fkg/l2",
        "l3_root": l3.state_root, "l4_run": l4.run_root, "publication": root / "publication",
        "workspace_id": WORKSPACE, "name_prefix": "p0", "publication_version": "local-v1",
        "out": tmp_path / "archive", "source_root": root / "source",
        "table": table, "record": record, "compilation": compilation,
    }
    deployed = deployment_fixture(p, tmp_path)
    binding = _bind(deployed)
    fake = FakeReader(deployed)
    p["domain"].write_text("approval: {status: draft, notes: []}\n")
    root.rename(tmp_path / "unavailable-producer")
    result = resolve_stored_record(
        **_resolve(deployed, binding), field=field,
        read_remote=True, approve_read=binding["binding_hash"], reader=fake,
    )
    assert result["field_value"] == 0
    assert result["stored_field_verified"] is True
    assert result["field_binding_status"] == result["resolution_status"] == "COMPLETE"
    assert len(result["assertions"]) == 1 and len(result["observations"]) == 1
    assert result["observations"][0]["source_bytes_status"] == "VERIFIED"
    assert result["live_fabric_verified"] is False


@pytest.mark.parametrize("change", ["tables_missing", "scalar_plan", "scalar_journal", "actions_missing"])
def test_invalid_publication_shape_is_an_explicit_error(deployed, change):
    if change == "tables_missing":
        plan = copy.deepcopy(deployed["plan_value"])
        del plan["tables"]
        deployed["plan"].write_text(json.dumps(plan))
    elif change == "scalar_plan":
        deployed["plan"].write_text("42")
    elif change == "scalar_journal":
        deployed["journal"].write_text("true")
    else:
        journal = copy.deepcopy(deployed["journal_value"])
        journal["actions"] = []
        deployed["journal"].write_text(json.dumps(journal))
    with pytest.raises(LineageArchiveError, match="INVALID_PUBLICATION_INPUT"):
        _bind(deployed)


def _publish_args(deployed):
    p = deployed["producer"]
    return [
        "publish-structured", "--l4-run", str(p["l4_run"]), "--l3-root", str(p["l3_root"]),
        "--workspace-id", WORKSPACE, "--name-prefix", "p0", "--prototype-create-only",
        "--live", "--approve-live", deployed["plan_value"]["plan_hash"],
        "--plan", str(deployed["plan"]), "--prototype-journal", str(deployed["journal"]),
        "--materialize", str(p["publication"]),
        "--lineage-archive", str(deployed["archive"]), "--lineage-archive-hash", deployed["archive_hash"],
        "--lineage-binding-out", str(deployed["out"]),
    ]


def test_publisher_optional_wiring_uses_verified_journal_after_success(deployed, monkeypatch):
    from fabric_kg_builder.cli.app_cmd import app_cmd
    import fabric_kg_builder.deploy.schema2_prototype as prototype

    calls = []

    def fake_publish(**kwargs):
        calls.append(kwargs)
        return {"plan_hash": deployed["plan_value"]["plan_hash"], **deployed["journal_value"]}

    monkeypatch.setattr(prototype, "publish_schema2_prototype", fake_publish)
    result = CliRunner().invoke(app_cmd, _publish_args(deployed))
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "lineage_binding_hash=" in result.output
    assert (deployed["out"] / "binding.json").exists()


def test_publisher_binding_preflight_rejects_bad_archive_before_any_publish(deployed, monkeypatch):
    from fabric_kg_builder.cli.app_cmd import app_cmd
    import fabric_kg_builder.deploy.schema2_prototype as prototype

    def forbidden(**kwargs):
        raise AssertionError("publisher must not run after invalid lineage preflight")

    monkeypatch.setattr(prototype, "publish_schema2_prototype", forbidden)
    args = _publish_args(deployed)
    args[args.index("--lineage-archive-hash") + 1] = "0" * 64
    result = CliRunner().invoke(app_cmd, args)
    assert result.exit_code != 0
    assert "ARCHIVE_HASH_MISMATCH" in result.output


def test_failed_postpublication_binding_never_redeploys_or_reports_success(deployed, monkeypatch):
    from fabric_kg_builder.cli.app_cmd import app_cmd
    import fabric_kg_builder.deploy.schema2_prototype as prototype

    calls = []

    def fake_publish(**kwargs):
        calls.append(kwargs)
        payload = copy.deepcopy(deployed["journal_value"])
        payload["status"] = "partial-retained"
        deployed["journal"].write_text(json.dumps(payload))
        return {"plan_hash": deployed["plan_value"]["plan_hash"], **payload}

    monkeypatch.setattr(prototype, "publish_schema2_prototype", fake_publish)
    result = CliRunner().invoke(app_cmd, _publish_args(deployed))
    assert result.exit_code != 0
    assert len(calls) == 1
    assert "resources retained; lineage binding was not created" in result.output
    assert "do not redeploy" in result.output
    assert "lineage_binding_hash=" not in result.output
    assert not deployed["out"].exists()


@pytest.mark.parametrize("change, message", [
    ("partial", "together"), ("dry_run", "requires --prototype-create-only --live"),
    ("nonprototype", "requires --prototype-create-only --live"), ("overlap", "OUTPUT_ROOT_COLLISION"),
])
def test_publisher_lineage_flags_fail_before_live_call(deployed, monkeypatch, change, message):
    from fabric_kg_builder.cli.app_cmd import app_cmd
    import fabric_kg_builder.deploy.schema2_prototype as prototype

    def forbidden(**kwargs):
        raise AssertionError("invalid opt-in must not reach publisher")

    monkeypatch.setattr(prototype, "publish_schema2_prototype", forbidden)
    args = _publish_args(deployed)
    if change == "partial":
        index = args.index("--lineage-archive-hash")
        del args[index:index + 2]
    elif change == "dry_run":
        args[args.index("--live")] = "--dry-run"
    elif change == "nonprototype":
        args.remove("--prototype-create-only")
    else:
        args[args.index("--lineage-binding-out") + 1] = str(deployed["producer"]["l4_run"] / "binding")
    result = CliRunner().invoke(app_cmd, args)
    assert result.exit_code != 0
    assert message.casefold() in result.output.casefold()
