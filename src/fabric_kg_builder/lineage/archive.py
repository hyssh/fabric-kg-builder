"""Explicit local provenance archives, independent of mutable producer paths.

An archive binds a default prototype materialization, not a live Fabric readback.
Only explicitly supplied local source roots and cache files are ever opened.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import pyarrow.parquet as pq

from fabric_kg_builder.contracts.base import canonical_json_bytes, canonical_sha256
from fabric_kg_builder.contracts.evidence import EvidenceSpanV1_1, SourceUnit
from fabric_kg_builder.sources.corpus import SourceCorpusEntry
from fabric_kg_builder.sources.docintel_cache import CachedLayout

ARCHIVE_VERSION = "local-lineage/1.0.0"
MAX_JSON_BYTES = 128 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024 * 1024


class LineageArchiveError(ValueError):
    """An archive cannot establish the requested immutable linkage."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise LineageArchiveError(f"INVALID_ID: {label} must be a nonempty string")
    return value


def _json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise LineageArchiveError(f"INVALID_JSON: duplicate key {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise LineageArchiveError(f"INVALID_JSON: nonfinite number {value}")

    if len(raw) > MAX_JSON_BYTES:
        raise LineageArchiveError("JSON_TOO_LARGE: exceeds 128 MiB")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LineageArchiveError(f"INVALID_JSON: {exc}") from exc


def _member(root: Path, relative: str) -> Path:
    _text(relative, "archive member")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts) or "\\" in relative:
        raise LineageArchiveError("UNSAFE_PATH: archive/source member is not relative")
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise LineageArchiveError("UNSAFE_PATH: member escaped explicit root")
    for path in (target, *target.parents):
        if path == root:
            break
        if path.is_symlink():
            raise LineageArchiveError("UNSAFE_PATH: symlink member refused")
    return target


def _read(path: Path, *, limit: int = MAX_SOURCE_BYTES) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise LineageArchiveError(f"ARTIFACT_TOO_LARGE: {path.name}")
    return raw


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _source_bytes(root: Path, entry: SourceCorpusEntry) -> tuple[str, bytes | None]:
    path = _member(root, entry.relative_source_ref)
    try:
        raw = _read(path)
    except FileNotFoundError:
        return "MISSING", None
    except PermissionError:
        return "DENIED", None
    if _sha(raw) != entry.original_byte_hash or len(raw) != entry.byte_count:
        return "VERSION_MISMATCH", None
    return "VERIFIED", raw


def create_archive(
    *,
    l1_state: Path,
    domain: Path,
    l2_state: Path,
    l3_root: Path,
    l4_run: Path,
    publication: Path,
    workspace_id: str,
    name_prefix: str,
    publication_version: str,
    out: Path,
    source_root: Path | None = None,
    layout_cache: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Create one immutable local snapshot; no remote reads or publication."""
    from fabric_kg_builder.contracts.receipts import StageReceipt
    from fabric_kg_builder.enrichment.schema2_sources import load_l2_inputs
    from fabric_kg_builder.enrichment.schema2_validation_stage import (
        l3_input_fingerprint, l3_run_root, load_l3_inputs, proposed_candidate_payload,
    )
    from fabric_kg_builder.lineage.schema2 import trace_schema2_record
    from fabric_kg_builder.semantic.derived_hierarchy_artifact import (
        _check_output_root_disjoint, _check_output_root_not_already_published,
        _exclusive_publish_rename,
    )
    from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
    from fabric_kg_builder.deploy.schema2_prototype import _compile, _table_proof
    from fabric_kg_builder.deploy.schema2_prototype_query import _evidence

    _text(publication_version, "publication_version")
    protected = {
        "L1": l1_state, "domain": domain, "L2": l2_state, "L3": l3_root,
        "L4": l4_run, "publication": publication,
        **{f"cache_{i}": path for i, path in enumerate(layout_cache)},
        **{f"default_{name}": Path(".fkg") / name for name in ("l1", "l2", "l3", "l4")},
        "default_release": Path("build/release"),
        "default_registry": Path("build/registry.json"),
    }
    if source_root is not None:
        protected["source"] = source_root
    if l4_run.parent.name == "runs":
        protected["L4_state"] = l4_run.parent.parent
    _check_output_root_disjoint(out, protected)
    _check_output_root_not_already_published(out)
    inputs = load_l3_inputs(
        l1_state_root=l1_state, domain_path=domain, l2_state_root=l2_state,
    )
    candidates = [
        (batch_id, candidate)
        for batch_id in inputs.leaf_batch_ids
        for candidate in inputs.proposed_partitions[batch_id]
    ]
    if not candidates:
        raise LineageArchiveError("EMPTY_EXTRACTION: no supported candidate lineage")
    # Reuse the production exact L1/L2/L3/L4 linkage checks, including relocation.
    trace = trace_schema2_record(
        candidates[0][1].candidate_id, l1_state=l1_state, domain=domain,
        l2_state=l2_state, l4_run=l4_run, l3_root=l3_root,
    )
    source = SealedL4ServingSource.from_run(
        l4_run, input_manifest_search_roots=(l3_root,),
    )
    evidence = _evidence(source, l3_root)
    for span in evidence.values():
        span.verify_against(inputs.source_units.require(span.source_unit_id))
    compilation = _compile(l4_run, l3_root, workspace_id, name_prefix)
    if {path.name for path in (publication / "tables").iterdir()} != {
        f"{name}.parquet" for name in compilation.tables
    } or {path.name for path in (publication / "definitions").iterdir()} != {
        f"{name}.json" for name in compilation.definitions
    }:
        raise LineageArchiveError("UNSUPPORTED_PUBLICATION_SCOPE: expected exactly the default table/definition set")
    files: dict[str, bytes] = {}
    for name, table in compilation.tables.items():
        relative = f"tables/{name}.parquet"
        raw = _read(_member(publication, relative))
        # Compare the exact bytes that will be archived, not a second file read.
        import pyarrow as pa

        if _table_proof(pq.read_table(pa.BufferReader(raw))) != _table_proof(table):
            raise LineageArchiveError(f"PUBLICATION_MISMATCH: {name}")
        files[relative] = raw
    for name, definition in compilation.definitions.items():
        relative = f"definitions/{name}.json"
        raw = _read(_member(publication, relative), limit=MAX_JSON_BYTES)
        if _json(raw) != definition:
            raise LineageArchiveError(f"PUBLICATION_MISMATCH: definition {name}")
        files[relative] = raw

    corpus = {
        entry.source_file_id: entry.model_dump(mode="json")
        for entry in inputs.corpus_manifest.entries
    }
    source_members: dict[str, str] = {}
    if source_root is not None:
        for entry in inputs.corpus_manifest.entries:
            status, raw = _source_bytes(source_root, entry)
            if status != "VERIFIED" or raw is None:
                raise LineageArchiveError(f"SOURCE_{status}: {entry.source_file_id}")
            relative = f"sources/{entry.original_byte_hash}"
            files[relative] = raw
            source_members[entry.source_file_id] = relative
    cache_members: dict[str, str] = {}
    for path in layout_cache:
        raw = _read(path, limit=MAX_JSON_BYTES)
        cached = CachedLayout.model_validate(_json(raw))
        if cached.input_sha256 not in {entry["original_byte_hash"] for entry in corpus.values()}:
            raise LineageArchiveError("CACHE_SOURCE_MISMATCH: cache is outside this source snapshot")
        relative = f"layouts/{cached.cache_key}.json"
        if relative in files:
            raise LineageArchiveError("DUPLICATE_CACHE: one cache per input/configuration")
        files[relative] = raw
        cache_members[cached.cache_key] = relative

    fingerprint = l3_input_fingerprint(inputs)
    l3_run = l3_root if l3_root.name == fingerprint else l3_run_root(l3_root, fingerprint)
    l1_inputs = load_l2_inputs(l1_state_root=l1_state, domain_path=domain)
    receipts = [
        l1_inputs.l1_receipt, inputs.l2_receipt,
        StageReceipt.model_validate_json((l3_run / "stage-receipt.json").read_bytes()),
        source.receipt,
    ]
    index = {
        "archive_version": ARCHIVE_VERSION,
        "publication_version": publication_version,
        "publication_scope": "local_materialization_only",
        "live_fabric_verified": False,
        "authority": trace["authority"],
        "domain": inputs.domain_contract.model_dump(mode="json"),
        "corpus": corpus,
        "source_members": source_members,
        "cache_members": cache_members,
        "source_units": [unit.model_dump(mode="json") for unit in inputs.source_units.units],
        "evidence": [span.model_dump(mode="json") for span in evidence.values()],
        "candidates": [{
            "candidate": proposed_candidate_payload(candidate), "batch_id": batch_id,
            "batch_hash": inputs.batch_by_id[batch_id].batch_hash,
        } for batch_id, candidate in candidates],
        "audit": list(source.audit_rows()),
        "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
        "manifests": [manifest.model_dump(mode="json") for manifest in (
            inputs.source_unit_manifest, inputs.l2_input_manifest,
            inputs.l2_output_manifest, source.input_manifest, source.manifest,
        )],
        "compilation": compilation.provenance,
        "tables": sorted(compilation.tables),
        "definitions": sorted(compilation.definitions),
        "process_coverage": "recorded_identities_and_receipts_only",
        "limitations": [
            "Not a live Delta version, remote archive, signature, or authorization grant.",
            "Unknown field contributors and unavailable raw locator mappings remain incomplete.",
            "Caller must protect the archive hash and configure retention/ACL separately.",
        ],
    }
    files["index.json"] = canonical_json_bytes(index) + b"\n"
    if len(files["index.json"]) > MAX_JSON_BYTES:
        raise LineageArchiveError("JSON_TOO_LARGE: archive index exceeds 128 MiB")
    manifest = {
        "archive_version": ARCHIVE_VERSION,
        "publication_version": publication_version,
        "entries": {
            relative: {"sha256": _sha(raw), "byte_count": len(raw)}
            for relative, raw in sorted(files.items())
        },
    }
    manifest["archive_hash"] = canonical_sha256(manifest)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".lineage-", dir=out.parent) as temp:
        staging = Path(temp) / "archive"
        staging.mkdir(mode=0o700)
        for relative, raw in {
            **files, "archive-manifest.json": canonical_json_bytes(manifest) + b"\n",
        }.items():
            target = _member(staging, relative)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as stream:
                os.chmod(target, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        _exclusive_publish_rename(staging, out)
    return {
        "archive": str(out), "archive_hash": manifest["archive_hash"],
        "publication_version": publication_version,
        "publication_scope": "local_materialization_only", "live_fabric_verified": False,
    }


def _open_archive(root: Path, expected_hash: str | None) -> tuple[dict[str, Any], dict[str, bytes]]:
    manifest = _json(_read(_member(root, "archive-manifest.json"), limit=MAX_JSON_BYTES))
    if not isinstance(manifest, dict) or manifest.get("archive_version") != ARCHIVE_VERSION:
        raise LineageArchiveError("UNSUPPORTED_ARCHIVE_VERSION")
    digest = manifest.get("archive_hash")
    if digest != canonical_sha256({k: v for k, v in manifest.items() if k != "archive_hash"}):
        raise LineageArchiveError("ARCHIVE_MANIFEST_MISMATCH")
    if expected_hash is not None and digest != expected_hash:
        raise LineageArchiveError("ARCHIVE_HASH_MISMATCH")
    entries = manifest.get("entries")
    if not isinstance(entries, dict) or "index.json" not in entries:
        raise LineageArchiveError("INVALID_ARCHIVE: missing index")
    files = {}
    for relative, proof in entries.items():
        if not isinstance(proof, dict):
            raise LineageArchiveError("INVALID_ARCHIVE: invalid member proof")
        # Source accessibility is reported separately from metadata integrity.
        if relative.startswith("sources/"):
            _member(root, relative)
            continue
        raw = _read(
            _member(root, relative),
            limit=MAX_JSON_BYTES if relative.endswith(".json") else MAX_SOURCE_BYTES,
        )
        if proof != {"sha256": _sha(raw), "byte_count": len(raw)}:
            raise LineageArchiveError(f"ARCHIVE_MEMBER_MISMATCH: {relative}")
        files[relative] = raw
    index = _json(files["index.json"])
    if (
        not isinstance(index, dict) or index.get("archive_version") != ARCHIVE_VERSION
        or index.get("publication_version") != manifest["publication_version"]
    ):
        raise LineageArchiveError("INVALID_ARCHIVE: index/version mismatch")
    index["archive_hash"] = digest
    return index, files


def _assertions(
    index: dict[str, Any], files: dict[str, bytes], table: str, record: str, field: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str, dict[str, Any]]:
    import pyarrow as pa

    if table not in index["tables"]:
        raise LineageArchiveError("TABLE_NOT_FOUND")

    def rows(name: str) -> list[dict[str, Any]]:
        return pq.read_table(pa.BufferReader(files[f"tables/{name}.parquet"])).to_pylist()

    key_by_table = {
        "l4_semantic_asserted_entities": "entity_id",
        "l4_semantic_asserted_relationships": "relationship_id",
        "l4_semantic_asserted_properties": "property_assertion_id",
    }
    key = key_by_table.get(table, "__canonical_id")
    matches = [row for row in rows(table) if row.get(key) == record]
    if len(matches) != 1:
        raise LineageArchiveError("RECORD_NOT_FOUND_OR_AMBIGUOUS")
    stored = matches[0]
    if field is not None and field not in stored:
        raise LineageArchiveError("FIELD_NOT_FOUND")
    entities = rows("l4_semantic_asserted_entities")
    relationships = rows("l4_semantic_asserted_relationships")
    properties = rows("l4_semantic_asserted_properties")
    assertions = (
        [row for row in entities if row["entity_id"] == record]
        + [row for row in relationships if row["relationship_id"] == record]
        + [row for row in properties if row["property_assertion_id"] == record]
    )
    transform: dict[str, Any] = {"kind": "identity_or_record_binding"}
    if not assertions:
        raise LineageArchiveError("UNSUPPORTED_TABLE: no canonical assertion binding")
    if field is None:
        return stored, assertions, "COMPLETE", transform
    if table in key_by_table:
        if field in {"normalized_value_json", "label"}:
            return stored, assertions, "INCOMPLETE_FIELD_TRANSFORM", transform
        return stored, assertions, "INCOMPLETE_ADMINISTRATIVE_FIELD", transform
    if field == "__canonical_id":
        return stored, assertions, "COMPLETE", transform
    definition = _json(files["definitions/parquet.json"])
    bindings = [
        prop for semantic_type in definition["semantic_types"]
        if semantic_type["physical_table_id"] == table
        for prop in semantic_type["properties"] if prop["physical_column_id"] == field
    ]
    if len(bindings) == 1:
        property_id = bindings[0]["canonical_property_id"]
        assertions = [
            row for row in properties
            if row["entity_id"] == record and row["semantic_property_id"] == property_id
        ]
        transform = {
            "kind": "normalized_property_to_typed_column",
            "binding": bindings[0],
            "contributor_selection": "single_assertion" if len(assertions) == 1 else "unknown",
        }
        return stored, assertions, (
            "COMPLETE" if len(assertions) == 1 else "INCOMPLETE_PROPERTY_CONTRIBUTORS"
        ), transform
    return stored, assertions, "INCOMPLETE_DERIVED_OR_UNMAPPED_FIELD", transform


def _observation(
    index: dict[str, Any], files: dict[str, bytes], archive: Path,
    source_root: Path | None, unit: SourceUnit, span: EvidenceSpanV1_1 | None,
) -> dict[str, Any]:
    entry = SourceCorpusEntry.model_validate(index["corpus"][unit.source_file_id])
    if span is not None:
        span.verify_against(unit)
        if span.asset_version_id != entry.asset_version_id:
            raise LineageArchiveError("ARCHIVE_ASSET_VERSION_MISMATCH")
    if source_root is not None:
        status, raw = _source_bytes(source_root, entry)
    elif unit.source_file_id in index["source_members"]:
        relative = index["source_members"][unit.source_file_id]
        try:
            raw = _read(_member(archive, relative))
            status = (
                "VERIFIED" if _sha(raw) == entry.original_byte_hash
                and len(raw) == entry.byte_count else "VERSION_MISMATCH"
            )
        except FileNotFoundError:
            status, raw = "MISSING", None
        except PermissionError:
            status, raw = "DENIED", None
    else:
        status, raw = "NOT_ARCHIVED", None
    from fabric_kg_builder.lineage.layout_bridge import raw_layout_location

    return {
        "evidence": span.model_dump(mode="json") if span is not None else None,
        "verified_assertion_evidence": span is not None,
        "source_unit": unit.model_dump(mode="json"),
        "source": entry.model_dump(mode="json"), "source_bytes_status": status,
        "source_bytes_verified": status == "VERIFIED",
        "source_byte_count_read": len(raw) if status == "VERIFIED" and raw is not None else None,
        "raw_location": raw_layout_location(
            unit, span, [
                CachedLayout.model_validate(_json(files[relative]))
                for relative in index["cache_members"].values()
            ], entry.original_byte_hash,
        ),
        "semantic_accuracy": "not_assessed",
    }


def resolve_archive(
    *, archive: Path, publication_version: str, table: str, record: str,
    field: str | None = None, source_root: Path | None = None,
    archive_hash: str | None = None,
) -> dict[str, Any]:
    """Resolve archive metadata and independently verify authorized source bytes."""
    for name, value in (("publication_version", publication_version), ("table", table), ("record", record)):
        _text(value, name)
    if field is not None:
        _text(field, "field")
    index, files = _open_archive(archive, archive_hash)
    if publication_version != index["publication_version"]:
        raise LineageArchiveError("PUBLICATION_VERSION_MISMATCH")
    audit_only = table == "audit_candidates"
    if audit_only:
        matches = [
            row for row in index["audit"]
            if record in (row["candidate_id"], row.get("input_candidate_id"))
        ]
        if not matches:
            raise LineageArchiveError("RECORD_NOT_FOUND: audit candidate")
        if field is not None:
            raise LineageArchiveError("UNSUPPORTED_FIELD: audit candidate has no serving fields")
        stored, assertions, field_status, transform = matches[0], [], "AUDIT_ONLY", {"kind": "proposal_only"}
    else:
        stored, assertions, field_status, transform = _assertions(index, files, table, record, field)
    units = {unit["source_unit_id"]: SourceUnit.model_validate(unit) for unit in index["source_units"]}
    spans = {
        span["evidence_span_id"]: EvidenceSpanV1_1.model_validate_json(canonical_json_bytes(span))
        for span in index["evidence"]
    }
    observations = []
    missing = []
    for evidence_id in sorted({item for row in assertions for item in row.get("evidence_span_ids", ())}):
        if evidence_id not in spans:
            missing.append(evidence_id)
            continue
        span = spans[evidence_id]
        unit = units.get(span.source_unit_id)
        if unit is None:
            raise LineageArchiveError("ARCHIVE_SOURCE_UNIT_MISSING")
        observations.append(_observation(index, files, archive, source_root, unit, span))
    candidate_ids = {item for row in assertions for item in row.get("candidate_ids", ())}
    assertion_ids = {
        row.get("property_assertion_id") or row.get("relationship_id") or row.get("entity_id")
        for row in assertions
    }
    audit = [
        row for row in index["audit"]
        if (
            row.get("candidate_id") in candidate_ids
            or row.get("semantic_assertion_id") in assertion_ids
            or audit_only and record in (row["candidate_id"], row.get("input_candidate_id"))
        )
    ]
    candidate_ids.update(row["candidate_id"] for row in audit)
    candidate_ids.update(row["input_candidate_id"] for row in audit if row.get("input_candidate_id"))
    proposals = [
        item for item in index["candidates"] if item["candidate"]["candidate_id"] in candidate_ids
    ]
    if audit_only:
        for unit_id in sorted({item["candidate"]["source_unit_id"] for item in proposals}):
            unit = units.get(unit_id)
            if unit is None:
                raise LineageArchiveError("ARCHIVE_SOURCE_UNIT_MISSING")
            observations.append(_observation(index, files, archive, source_root, unit, None))
    transform["extraction_observations"] = [{
        "candidate_id": item["candidate"]["candidate_id"],
        "value_json": item["candidate"].get("value_json"),
        "normalized_value_json": item["candidate"].get("normalized_value_json"),
        "normalization_recipe": "not_recorded",
    } for item in proposals if item["candidate"].get("candidate_kind") == "property"]
    if field is not None and field_status == "COMPLETE" and any(
        item["value_json"] != item["normalized_value_json"]
        for item in transform["extraction_observations"]
    ):
        field_status = "INCOMPLETE_EXTRACTION_NORMALIZATION"
    return {
        "archive_version": ARCHIVE_VERSION, "archive_hash": index["archive_hash"],
        "publication_version": publication_version, "publication_scope": index["publication_scope"],
        "live_fabric_verified": False, "record": stored, "field": field,
        "field_value": stored.get(field) if field is not None else None,
        "field_binding_status": field_status, "field_transform": transform,
        "assertions": assertions, "observations": observations, "missing_evidence_ids": missing,
        "metadata_status": "COMPLETE" if observations and not missing else "INCOMPLETE",
        "resolution_status": "AUDIT_ONLY" if audit_only else (
            "COMPLETE" if observations and not missing and field_status == "COMPLETE" else "INCOMPLETE"
        ),
        "original_source_retrieval_verified": bool(observations) and all(
            item["source_bytes_verified"] for item in observations
        ),
        "authority": index["authority"], "audit": audit,
        "candidates": proposals,
        "process_receipts": index["receipts"], "compilation": index["compilation"],
        "process_coverage": index["process_coverage"],
        "publication_authority": False,
    }
