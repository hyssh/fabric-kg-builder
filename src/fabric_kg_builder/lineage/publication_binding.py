"""Deployed prototype identity -> trusted archive index -> historical provenance.

Binding construction is offline evidence validation, not remote attestation.
Only an explicit approved read may instantiate the OneLake read-only backend.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Protocol
import uuid

import pyarrow as pa
import pyarrow.parquet as pq

from fabric_kg_builder.contracts.base import canonical_json_bytes, canonical_sha256
from fabric_kg_builder.lineage.archive import (
    LineageArchiveError, MAX_JSON_BYTES, _json, _member, _open_archive, _read, _sha,
    _text, resolve_archive,
)

BINDING_VERSION = "prototype-lineage-binding/1.0.0"
MAX_READBACK_ROWS = 100_000
HISTORY_LIMIT = 100
TABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,255}\Z")


def _digest(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise LineageArchiveError("INVALID_HASH: expected lowercase SHA256")
    return value


def _guid(value: str) -> str:
    if not isinstance(value, str):
        raise LineageArchiveError("INVALID_TARGET_ID: expected canonical UUID")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError as exc:
        raise LineageArchiveError("INVALID_TARGET_ID: expected canonical UUID") from exc
    if value != parsed:
        raise LineageArchiveError("INVALID_TARGET_ID: expected canonical lowercase UUID")
    return value


def _table_name(value: str) -> str:
    if not isinstance(value, str) or TABLE_NAME.fullmatch(value) is None:
        raise LineageArchiveError("UNSAFE_TABLE_NAME")
    return value


def _delta_path(workspace: str, lakehouse: str, table: str) -> str:
    return (
        f"abfss://{_guid(workspace)}@onelake.dfs.fabric.microsoft.com"
        f"/{_guid(lakehouse)}/Tables/dbo/{_table_name(table)}"
    )


def _proof(table: pa.Table) -> dict[str, Any]:
    # Same typed multiset proof as the publisher, imported only after serving
    # package initialization to preserve cold-consumer import independence.
    from fabric_kg_builder.enrichment import schema2_sources  # noqa: F401
    from fabric_kg_builder.deploy.schema2_prototype import _table_proof

    return _table_proof(table)


def _same(left: Any, right: Any) -> bool:
    return canonical_sha256(left) == canonical_sha256(right)


def _validate_archive_plan(
    index: dict[str, Any], files: dict[str, bytes], plan: dict[str, Any],
) -> None:
    from fabric_kg_builder.enrichment import schema2_sources  # noqa: F401
    from fabric_kg_builder.deploy.schema2_prototype_reconcile import _valid_plan

    if (
        not isinstance(plan, dict)
        or not isinstance(plan.get("tables"), dict)
        or not isinstance(plan.get("names"), dict)
        or not isinstance(plan.get("provenance"), dict)
        or not isinstance(plan["provenance"].get("definition_hashes"), dict)
    ):
        raise LineageArchiveError("INVALID_PUBLICATION_INPUT: missing typed plan mappings")
    _text(plan.get("run_id"), "prototype run_id")
    _text(plan["names"].get("lakehouse"), "Lakehouse name")
    _valid_plan(plan)
    if not _same(plan.get("provenance"), index["compilation"]) or set(plan["tables"]) != set(index["tables"]):
        raise LineageArchiveError("PUBLICATION_ARCHIVE_MISMATCH: scope/projection/crosswalk differs")
    for kind in index["definitions"]:
        actual = canonical_sha256(_json(files[f"definitions/{kind}.json"]))
        if plan["provenance"]["definition_hashes"].get(kind) != actual:
            raise LineageArchiveError("PUBLICATION_ARCHIVE_MISMATCH: definitions differ")
    for table in index["tables"]:
        _table_name(table)
        if not _same(plan["tables"][table], _proof(pq.read_table(pa.BufferReader(files[f"tables/{table}.parquet"])))):
            raise LineageArchiveError("PUBLICATION_ARCHIVE_MISMATCH: typed table values differ")
    _guid(plan["workspace_id"])


def _validate_publication(
    index: dict[str, Any], files: dict[str, bytes],
    plan: dict[str, Any], journal: dict[str, Any],
) -> dict[str, Any]:
    from fabric_kg_builder.enrichment import schema2_sources  # noqa: F401
    from fabric_kg_builder.deploy.schema2_prototype import FORMAT_VERSION, STRUCTURAL_PUBLICATION

    _validate_archive_plan(index, files, plan)
    if (
        not isinstance(journal, dict) or not isinstance(journal.get("actions"), dict)
        or not isinstance(journal.get("item_ids"), dict)
        or not isinstance(journal["actions"].get("create:lakehouse"), dict)
        or any(not isinstance(action, dict) for action in journal["actions"].values())
    ):
        raise LineageArchiveError("INVALID_PUBLICATION_INPUT: missing typed journal actions")
    if (
        journal.get("journal_version") != FORMAT_VERSION
        or journal.get("plan_hash") != plan["plan_hash"]
        or journal.get("run_id") != plan["run_id"]
        or journal.get("workspace_id") != plan["workspace_id"]
        or journal.get("policy") != "create-only-retain-partial"
        or journal.get("status") != "structural-verification-complete"
        or journal.get("structural_publication") != STRUCTURAL_PUBLICATION
        or journal.get("ontology_readiness") != "companion-bindings-scalars-and-endpoint-pairs-verified"
    ):
        raise LineageArchiveError("PUBLICATION_NOT_VERIFIED: exact completed prototype journal required")
    actions = journal.get("actions", {})
    create = actions.get("create:lakehouse", {})
    lakehouse = _guid(create.get("item_id"))
    workspace = _guid(plan["workspace_id"])
    metadata = create.get("metadata", {})
    if not isinstance(metadata, dict) or not isinstance(metadata.get("properties"), dict):
        raise LineageArchiveError("LAKEHOUSE_IDENTITY_UNVERIFIED: metadata missing")
    if (
        create.get("status") != "identity-verified"
        or create.get("ownership") == "operator-reconciled"
        or create.get("http_status") not in (200, 201, 202)
        or create.get("http_status") == 202 and create.get("operation_state") != "Succeeded"
        or metadata.get("id") != lakehouse or metadata.get("type") != "Lakehouse"
        or metadata.get("displayName") != plan["names"]["lakehouse"]
        or metadata.get("properties", {}).get("defaultSchema") != "dbo"
        or journal.get("item_ids", {}).get("lakehouse") != lakehouse
    ):
        raise LineageArchiveError("LAKEHOUSE_IDENTITY_UNVERIFIED: returned-ID default-schema prototype required")
    if {key.removeprefix("delta:") for key in actions if key.startswith("delta:")} != set(index["tables"]):
        raise LineageArchiveError("DELTA_SCOPE_MISMATCH")
    tables = {}
    for table in sorted(index["tables"]):
        expected = _proof(pq.read_table(pa.BufferReader(files[f"tables/{table}.parquet"])))
        action = actions[f"delta:{table}"]
        path = _delta_path(workspace, lakehouse, table)
        if (
            action.get("status") != "verified"
            or type(action.get("delta_version")) is not int or action["delta_version"] != 0
            or action.get("path") != path or not _same(action.get("table_proof"), expected)
            or not _same(action.get("readback"), expected) or not _same(plan["tables"][table], expected)
        ):
            raise LineageArchiveError(f"DELTA_BINDING_MISMATCH: {table}")
        tables[table] = {"path": path, "delta_version": 0, "table_proof": expected}
    return {
        "workspace_id": workspace, "lakehouse_id": lakehouse, "schema": "dbo", "tables": tables,
        "plan_hash": plan["plan_hash"], "run_id": plan["run_id"],
    }


def preflight_publication_binding(
    *, archive: Path, archive_hash: str, plan: Path, journal: Path, out: Path,
    protected_roots: dict[str, Path] | None = None,
) -> None:
    """Validate opt-in publisher wiring before any live operation, without writes."""
    from fabric_kg_builder.enrichment import schema2_sources  # noqa: F401
    from fabric_kg_builder.semantic.derived_hierarchy_artifact import (
        _check_output_root_disjoint, _check_output_root_not_already_published,
    )

    index, files = _open_archive(archive, _digest(archive_hash))
    payload = _json(_read(plan, limit=MAX_JSON_BYTES))
    if not isinstance(payload, dict):
        raise LineageArchiveError("INVALID_PUBLICATION_INPUT: plan must be a JSON object")
    _validate_archive_plan(index, files, payload)
    _check_output_root_disjoint(out, {
        "archive": archive, "plan": plan, "journal": journal,
        "release_state": Path("build/release"), "registry": Path("build/registry.json"),
        **{f"stage_{stage}": Path(".fkg") / stage for stage in ("l1", "l2", "l3", "l4")},
        **(protected_roots or {}),
    })
    _check_output_root_not_already_published(out)


def bind_publication(
    *, archive: Path, archive_hash: str, plan: Path, journal: Path, out: Path,
) -> dict[str, Any]:
    """Create-only external-index boundary; no credentials, model or cloud call."""
    from fabric_kg_builder.enrichment import schema2_sources  # noqa: F401
    from fabric_kg_builder.semantic.derived_hierarchy_artifact import (
        _check_output_root_disjoint, _check_output_root_not_already_published, _exclusive_publish_rename,
    )

    index, files = _open_archive(archive, _digest(archive_hash))
    plan_raw, journal_raw = _read(plan, limit=MAX_JSON_BYTES), _read(journal, limit=MAX_JSON_BYTES)
    plan_value, journal_value = _json(plan_raw), _json(journal_raw)
    if not isinstance(plan_value, dict) or not isinstance(journal_value, dict):
        raise LineageArchiveError("INVALID_PUBLICATION_INPUT: plan/journal must be JSON objects")
    publication = _validate_publication(index, files, plan_value, journal_value)
    protected = {
        "archive": archive, "plan": plan, "journal": journal,
        "release_state": Path("build/release"), "registry": Path("build/registry.json"),
        **{f"stage_{stage}": Path(".fkg") / stage for stage in ("l1", "l2", "l3", "l4")},
    }
    _check_output_root_disjoint(out, protected)
    _check_output_root_not_already_published(out)
    binding = {
        "binding_version": BINDING_VERSION, "archive_hash": archive_hash,
        "archive_publication_version": index["publication_version"],
        "publication": publication,
        "evidence_files": {
            "plan.json": {"sha256": _sha(plan_raw), "byte_count": len(plan_raw)},
            "journal.json": {"sha256": _sha(journal_raw), "byte_count": len(journal_raw)},
        },
        "trust_basis": "caller-approved-external-binding-hash-and-journal-evidence",
        "remote_readback_performed_by_binding": False,
        "publication_authority": False,
    }
    binding["binding_hash"] = canonical_sha256(binding)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".lineage-binding-", dir=out.parent) as temp:
        staging = Path(temp) / "binding"
        staging.mkdir(mode=0o700)
        for relative, raw in {
            "binding.json": canonical_json_bytes(binding) + b"\n",
            "plan.json": plan_raw, "journal.json": journal_raw,
        }.items():
            path = staging / relative
            with path.open("xb") as stream:
                os.chmod(path, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        _exclusive_publish_rename(staging, out)
    return {"binding": str(out), **binding}


def _load_binding(
    binding: Path, binding_hash: str, archive: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    value = _json(_read(_member(binding, "binding.json"), limit=MAX_JSON_BYTES))
    if not isinstance(value, dict) or value.get("binding_version") != BINDING_VERSION:
        raise LineageArchiveError("UNSUPPORTED_BINDING_VERSION")
    if not isinstance(value.get("evidence_files"), dict) or not isinstance(value.get("publication"), dict):
        raise LineageArchiveError("INVALID_PUBLICATION_BINDING")
    if (
        value.get("trust_basis") != "caller-approved-external-binding-hash-and-journal-evidence"
        or value.get("remote_readback_performed_by_binding") is not False
        or value.get("publication_authority") is not False
    ):
        raise LineageArchiveError("INVALID_PUBLICATION_BINDING: unsupported trust basis")
    _text(value.get("archive_publication_version"), "archive snapshot version")
    expected = _digest(binding_hash)
    if (
        value.get("binding_hash") != expected
        or canonical_sha256({key: item for key, item in value.items() if key != "binding_hash"}) != expected
    ):
        raise LineageArchiveError("PUBLICATION_BINDING_HASH_MISMATCH")
    evidence = {}
    for name in ("plan.json", "journal.json"):
        raw = _read(_member(binding, name), limit=MAX_JSON_BYTES)
        if value["evidence_files"].get(name) != {"sha256": _sha(raw), "byte_count": len(raw)}:
            raise LineageArchiveError("PUBLICATION_EVIDENCE_HASH_MISMATCH")
        evidence[name] = _json(raw)
    index, files = _open_archive(archive, _digest(value.get("archive_hash")))
    publication = _validate_publication(index, files, evidence["plan.json"], evidence["journal.json"])
    if (
        not _same(publication, value["publication"])
        or index["publication_version"] != value["archive_publication_version"]
    ):
        raise LineageArchiveError("PUBLICATION_BINDING_CONTENT_MISMATCH")
    return value, index


@dataclass(frozen=True)
class DeltaReadback:
    table: pa.Table
    delta_version: int
    commit: dict[str, Any]


class PublicationReader(Protocol):
    def read(self, path: str, *, version: int, max_rows: int) -> DeltaReadback: ...


class OneLakeDeltaReader:
    """No mutation API; token and OneLake reads occur only inside read()."""

    def read(self, path: str, *, version: int, max_rows: int) -> DeltaReadback:
        from azure.identity import AzureCliCredential
        from deltalake import DeltaTable

        with AzureCliCredential() as credential:
            token = credential.get_token("https://storage.azure.com/.default").token
            options = {
                "bearer_token": token, "use_fabric_endpoint": "true",
                "timeout": "90s", "connect_timeout": "10s",
            }
            # delta-rs can number history relative to a time-travel snapshot.
            # Read history from the current log head, never latest row data.
            head = DeltaTable(path, without_files=True, storage_options=options)
            commits = [item for item in head.history(HISTORY_LIMIT) if item.get("version") == version]
            if len(commits) != 1:
                raise LineageArchiveError("DELTA_COMMIT_UNAVAILABLE_WITHIN_HISTORY_BOUND")
            delta = DeltaTable(path, version=version, storage_options=options)
            table = delta.to_pyarrow_dataset().head(max_rows + 1)
            return DeltaReadback(table=table, delta_version=delta.version(), commit=commits[0])


def resolve_stored_record(
    *, binding: Path, binding_hash: str, archive: Path, workspace_id: str,
    lakehouse_id: str, table: str, delta_version: int, record: str,
    field: str | None = None, source_root: Path | None = None,
    read_remote: bool = False, approve_read: str | None = None,
    max_rows: int = 10_000, reader: PublicationReader | None = None,
) -> dict[str, Any]:
    """Resolve pinned deployed identity; compare exact Delta commit/data on opt-in."""
    _text(record, "record")
    if field is not None:
        _text(field, "field")
    if type(delta_version) is not int or delta_version < 0:
        raise LineageArchiveError("INVALID_DELTA_VERSION")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_READBACK_ROWS:
        raise LineageArchiveError("INVALID_READBACK_BOUND")
    value, _ = _load_binding(binding, binding_hash, archive)
    publication = value["publication"]
    expected = publication["tables"].get(_table_name(table))
    if (
        _guid(workspace_id) != publication["workspace_id"]
        or _guid(lakehouse_id) != publication["lakehouse_id"]
        or expected is None or expected["delta_version"] != delta_version
    ):
        raise LineageArchiveError("STORED_PUBLICATION_IDENTITY_MISMATCH")
    if expected["path"] != _delta_path(workspace_id, lakehouse_id, table):
        raise LineageArchiveError("UNSAFE_DELTA_PATH")
    result = resolve_archive(
        archive=archive, archive_hash=value["archive_hash"],
        publication_version=value["archive_publication_version"], table=table,
        record=record, field=field, source_root=source_root,
    )
    result.update(
        binding_hash=binding_hash, stored_publication={
            "workspace_id": workspace_id, "lakehouse_id": lakehouse_id, "schema": "dbo",
            "table": table, "delta_version": delta_version,
            "plan_hash": publication["plan_hash"], "run_id": publication["run_id"],
        },
        stored_readback_status="NOT_RUN", stored_record_verified=False,
        live_fabric_verified=False, binding_trust_basis=value["trust_basis"],
    )
    if not read_remote:
        if approve_read is not None or reader is not None:
            raise LineageArchiveError("EXPLICIT_REMOTE_READ_REQUIRED")
        return result
    if approve_read != binding_hash:
        raise LineageArchiveError("EXACT_READ_APPROVAL_REQUIRED")
    if expected["table_proof"]["row_count"] > max_rows:
        raise LineageArchiveError("READBACK_ROW_CAP_EXCEEDED: no partial-table success")
    backend = reader if reader is not None else OneLakeDeltaReader()
    from azure.core.exceptions import AzureError
    from deltalake.exceptions import DeltaError

    try:
        observed = backend.read(expected["path"], version=delta_version, max_rows=max_rows)
    except (OSError, RuntimeError, ValueError, AzureError, DeltaError, pa.ArrowException) as exc:
        # Never emit backend exception strings that may contain storage credentials.
        raise LineageArchiveError(f"STORED_READ_FAILED: {type(exc).__name__}; no fallback") from None
    commit = observed.commit
    if (
        observed.delta_version != delta_version or type(observed.delta_version) is not int
        or type(commit.get("version")) is not int or commit["version"] != delta_version
        or commit.get("prototype_run_id") != publication["run_id"]
        or commit.get("prototype_plan_hash") != publication["plan_hash"]
        or commit.get("prototype_action") != f"delta:{table}"
    ):
        raise LineageArchiveError("DELTA_COMMIT_OWNERSHIP_MISMATCH")
    if observed.table.num_rows > max_rows or not _same(_proof(observed.table), expected["table_proof"]):
        raise LineageArchiveError("STORED_TABLE_READBACK_MISMATCH")
    keys = {
        "l4_semantic_asserted_entities": "entity_id",
        "l4_semantic_asserted_relationships": "relationship_id",
        "l4_semantic_asserted_properties": "property_assertion_id",
    }
    rows = [row for row in observed.table.to_pylist() if row.get(keys.get(table, "__canonical_id")) == record]
    if len(rows) != 1 or canonical_sha256(rows[0]) != canonical_sha256(result["record"]):
        raise LineageArchiveError("STORED_RECORD_READBACK_MISMATCH")
    result.update(
        stored_readback_status="VERIFIED", stored_record_verified=True,
        live_fabric_verified=type(backend) is OneLakeDeltaReader,
        readback_backend="onelake-delta" if type(backend) is OneLakeDeltaReader else "injected-reader",
        verified_commit={
            key: commit[key] for key in ("version", "prototype_run_id", "prototype_plan_hash", "prototype_action")
        },
        stored_field_verified=field is not None,
        verification_scope="exact-pinned-Delta-table-and-record; not Ontology query or semantic truth",
    )
    return result
