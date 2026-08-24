"""Unit tests for the compile-data CLI command.

Tests written per SPEC-001 §7 compile-data contract and SPEC-002 §9
VAL-001..VAL-007 data-integrity gates.

Fixtures
--------
- ``_clean_fixture`` — minimal canonical enriched JSON, all IDs unique, FKs valid
- ``_dup_entity_fixture`` — same entity_id in two rows → triggers VAL-001
- ``_dangling_fk_fixture`` — relationship with non-existent source_entity_id → VAL-005
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.compile_data_cmd import compile_data_cmd
from fabric_kg_builder.cli.compile_data_cmd import _load_schema2_projection_authority
from fabric_kg_builder.domain.models import ApprovalMetadataV2, DomainContractV2
from fabric_kg_builder.domain.proposal import DomainProposal
from fabric_kg_builder.domain.guard import write_domain_run_manifest
from fabric_kg_builder.domain.service import (
    compute_contract_hash,
    load_domain_contract,
    save_domain_contract,
)
from tests.conftest import (  # noqa: F401
    combined_output,
    make_cli_runner,
    write_approved_domain_contract,
)
from fabric_kg_builder.model.ids import (
    content_hash,
    make_chunk_id,
    make_entity_id,
    make_evidence_id,
    make_relationship_id,
)
from fabric_kg_builder.validate.data_gates import run_gates

_UTC = timezone.utc
_NOW = "2026-06-24T12:00:00+00:00"
_PROPOSAL_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "domain_proposals"
    / "facility_maintenance_proposal.json"
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_entity_row(
    entity_type: str = "Device",
    display_name: str = "Surface Laptop 5",
    source_file_id: str = "src:abc123",
    *,
    override_entity_id: str | None = None,
) -> dict:
    entity_id = override_entity_id or make_entity_id(entity_type, display_name)
    ck = f"{entity_type.lower()}:{display_name.lower().replace(' ', '-')}"
    ch = content_hash(ck)
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "display_name": display_name,
        "canonical_key": ck,
        "aliases": [],
        "search_aliases": None,
        "description": None,
        "properties_json": None,
        "source_file_id": source_file_id,
        "confidence": 0.9,
        "is_placeholder": False,
        "content_hash": ch,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _make_relationship_row(
    source_entity_id: str,
    target_entity_id: str,
    rel_type: str = "HAS_COMPONENT",
    evidence_id: str | None = None,
) -> dict:
    rel_id = make_relationship_id(rel_type, source_entity_id, target_entity_id)
    ch = content_hash(f"{rel_type}:{source_entity_id}:{target_entity_id}")
    return {
        "relationship_id": rel_id,
        "relationship_type": rel_type,
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "evidence_id": evidence_id,
        "properties_json": None,
        "confidence": 0.85,
        "is_placeholder": False,
        "content_hash": ch,
        "created_at": _NOW,
    }


def _make_chunk_row(source_file_id: str = "src:abc123") -> dict:
    text = "Surface Laptop 5 has a replaceable battery."
    ch = content_hash(text)
    cid = make_chunk_id(source_file_id, "section_text", ch)
    return {
        "chunk_id": cid,
        "source_file_id": source_file_id,
        "document_element_id": None,
        "chunk_type": "section_text",
        "content": text,
        "content_html": None,
        "embedding_text": text,
        "blob_url": None,
        "page_number": None,
        "section_path": "Repair > Battery",
        "table_id": None,
        "figure_id": None,
        "image_id": None,
        "related_entity_ids": None,
        "entity_search_keys": None,
        "content_hash": ch,
        "created_at": _NOW,
    }


def _make_evidence_row(source_file_id: str = "src:abc123") -> dict:
    ev_id = make_evidence_id(source_file_id, "document_span", "1:0:1", content_hash("evidence text"))
    ch = content_hash("evidence text")
    return {
        "evidence_id": ev_id,
        "source_file_id": source_file_id,
        "source_type": "document_span",
        "document_element_id": None,
        "chunk_id": None,
        "page_number": 1,
        "section_path": None,
        "table_id": None,
        "row_index": None,
        "col_index": None,
        "figure_id": None,
        "image_id": None,
        "callout_id": None,
        "visual_region_id": None,
        "blob_url": None,
        "text": "evidence text",
        "content_hash": ch,
        "created_at": _NOW,
    }


def _clean_fixture() -> dict:
    """Minimal clean fixture: 2 entities, 1 relationship, 1 chunk, 1 evidence."""
    e1 = _make_entity_row("Device", "Surface Laptop 5")
    e2 = _make_entity_row("Component", "Battery")
    rel = _make_relationship_row(e1["entity_id"], e2["entity_id"])
    chunk = _make_chunk_row()
    evidence = _make_evidence_row()
    return {
        "source_file_id": "src:abc123",
        "pass": "p2",
        "entities": [e1, e2],
        "relationships": [rel],
        "chunks": [chunk],
        "evidence": [evidence],
    }


def _dup_entity_fixture() -> dict:
    """Fixture with duplicate entity_id — triggers VAL-001."""
    e1 = _make_entity_row("Device", "Surface Laptop 5")
    # Second row with SAME entity_id but different type  
    e2 = _make_entity_row("Device", "Surface Laptop 5")  # same ID
    e2["entity_type"] = "Product"  # mutate type — same ID, different content
    chunk = _make_chunk_row()
    return {
        "source_file_id": "src:dup",
        "pass": "p2",
        "entities": [e1, e2],
        "relationships": [],
        "chunks": [chunk],
        "evidence": [],
    }


def _dangling_fk_fixture() -> dict:
    """Fixture with a relationship pointing to a non-existent entity_id — VAL-005."""
    e1 = _make_entity_row("Device", "Surface Laptop 5")
    rel = _make_relationship_row(
        source_entity_id=e1["entity_id"],
        target_entity_id="entity:nonexistent_does_not_exist",
    )
    chunk = _make_chunk_row()
    return {
        "source_file_id": "src:dangle",
        "pass": "p2",
        "entities": [e1],
        "relationships": [rel],
        "chunks": [chunk],
        "evidence": [],
    }


# ---------------------------------------------------------------------------
# Helper: write fixture to a tmp input dir
# ---------------------------------------------------------------------------


def _write_input(tmp_path: Path, fixture: dict, filename: str = "batch_p2.json") -> Path:
    input_dir = tmp_path / "enriched"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / filename).write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    return input_dir


def _write_schema2_authority(tmp_path: Path) -> tuple[Path, Path]:
    proposal = DomainProposal.model_validate(
        json.loads(_PROPOSAL_FIXTURE.read_text(encoding="utf-8"))
    )
    contract_hash = compute_contract_hash(proposal.contract)
    contract = proposal.contract.model_copy(
        update={
            "approval": ApprovalMetadataV2(
                status="approved",
                approved_by="test@example.com",
                approved_at_utc="2026-08-24T00:00:00Z",
                contract_hash=contract_hash,
                proposal_hash=proposal.proposal_hash,
                source_profile_hash=proposal.source_profile_hash,
                prompt_hash=proposal.prompt_hash,
                prompt_version=proposal.prompt_version,
                model_version=proposal.model_version,
                model_hash=proposal.model_hash,
            )
        }
    )
    contract_path = tmp_path / "domain.yaml"
    save_domain_contract(contract, contract_path)
    input_dir = tmp_path / "build" / "enriched"
    input_dir.mkdir(parents=True)
    (input_dir / "domain.run-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "domain_contract": {
                    "path": "domain.yaml",
                    "contract_hash": contract_hash,
                    "approval_status": "approved",
                    "approved_by": contract.approval.approved_by,
                    "approved_at_utc": contract.approval.approved_at_utc,
                    "schema_version": "2.0",
                    "prompt_version": contract.approval.prompt_version,
                    "prompt_hash": contract.approval.prompt_hash,
                    "model_version": contract.approval.model_version,
                    "model_hash": contract.approval.model_hash,
                    "proposal_hash": contract.approval.proposal_hash,
                    "source_profile_hash": contract.approval.source_profile_hash,
                },
            }
        ),
        encoding="utf-8",
    )
    return input_dir, contract_path


def test_schema2_authority_resolves_project_relative_manifest_path(
    tmp_path: Path,
) -> None:
    input_dir, contract_path = _write_schema2_authority(tmp_path)
    contract, manifest = _load_schema2_projection_authority(input_dir)
    assert contract is not None
    assert manifest is not None
    assert compute_contract_hash(contract) == manifest["domain_contract"]["contract_hash"]
    assert contract_path.exists()


def test_explicit_historical_schema1_manifest_without_contract_is_compatible(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "build" / "enriched"
    input_dir.mkdir(parents=True)
    (input_dir / "domain.run-manifest.json").write_text(
        json.dumps({"schema_version": "1.0"}),
        encoding="utf-8",
    )
    contract, manifest = _load_schema2_projection_authority(input_dir)
    assert contract is None
    assert manifest == {"schema_version": "1.0"}


def test_referenced_schema1_contract_is_inspected_before_compatibility(
    tmp_path: Path,
) -> None:
    contract_path = write_approved_domain_contract(tmp_path / "domain.yaml")
    contract = load_domain_contract(contract_path)
    input_dir = tmp_path / "build" / "enriched"
    input_dir.mkdir(parents=True)
    manifest = {
        "schema_version": "1.0",
        "domain_contract": {
            "path": "domain.yaml",
            "schema_version": "1.0",
            "contract_hash": compute_contract_hash(contract),
        },
    }
    (input_dir / "domain.run-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    authority, loaded_manifest = _load_schema2_projection_authority(input_dir)
    assert authority is None
    assert loaded_manifest == manifest


def test_schema2_authority_rejects_path_outside_project(tmp_path: Path) -> None:
    input_dir, _ = _write_schema2_authority(tmp_path)
    manifest_path = input_dir / "domain.run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["domain_contract"]["path"] = str(_PROPOSAL_FIXTURE)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Exception, match="outside the trusted project root"):
        _load_schema2_projection_authority(input_dir)


def test_schema2_authority_rejects_stale_manifest_hash(tmp_path: Path) -> None:
    input_dir, _ = _write_schema2_authority(tmp_path)
    manifest_path = input_dir / "domain.run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["domain_contract"]["contract_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Exception, match="contract hash mismatch"):
        _load_schema2_projection_authority(input_dir)


def test_schema2_marker_without_domain_contract_fails_closed(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "build" / "enriched"
    input_dir.mkdir(parents=True)
    (input_dir / "domain.run-manifest.json").write_text(
        json.dumps({"schema_version": "2.0"}),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="missing a referenced contract"):
        _load_schema2_projection_authority(input_dir)


def test_conflicting_schema_markers_never_downgrade_to_schema1(
    tmp_path: Path,
) -> None:
    input_dir, _ = _write_schema2_authority(tmp_path)
    manifest_path = input_dir / "domain.run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "1.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Exception, match="referenced schema-2 contract"):
        _load_schema2_projection_authority(input_dir)


def test_schema2_approval_marker_with_schema1_versions_fails_closed(
    tmp_path: Path,
) -> None:
    input_dir, _ = _write_schema2_authority(tmp_path)
    manifest_path = input_dir / "domain.run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "1.0"
    manifest["domain_contract"]["schema_version"] = "1.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Exception, match="referenced schema-2 contract"):
        _load_schema2_projection_authority(input_dir)


def test_referenced_v2_contract_cannot_be_downgraded_by_manifest_markers(
    tmp_path: Path,
) -> None:
    input_dir, _ = _write_schema2_authority(tmp_path)
    manifest_path = input_dir / "domain.run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "1.0"
    manifest["domain_contract"]["schema_version"] = "1.0"
    del manifest["domain_contract"]["proposal_hash"]
    del manifest["domain_contract"]["source_profile_hash"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Exception, match="referenced schema-2 contract"):
        _load_schema2_projection_authority(input_dir)


def test_schema2_manifest_requires_complete_approval_binding(
    tmp_path: Path,
) -> None:
    input_dir, _ = _write_schema2_authority(tmp_path)
    manifest_path = input_dir / "domain.run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["domain_contract"]["proposal_hash"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Exception, match="approval bindings"):
        _load_schema2_projection_authority(input_dir)


@pytest.mark.parametrize("field", ["prompt_hash", "model_hash"])
def test_schema2_manifest_rejects_prompt_or_model_hash_tampering(
    tmp_path: Path,
    field: str,
) -> None:
    input_dir, _ = _write_schema2_authority(tmp_path)
    manifest_path = input_dir / "domain.run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["domain_contract"][field] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Exception, match="approval bindings"):
        _load_schema2_projection_authority(input_dir)


def test_domain_run_manifest_persists_prompt_and_model_hashes(
    tmp_path: Path,
) -> None:
    _, contract_path = _write_schema2_authority(tmp_path)
    contract = load_domain_contract(contract_path)
    assert isinstance(contract, DomainContractV2)
    output_dir = tmp_path / "manifest-output"
    output_dir.mkdir()
    manifest_path = write_domain_run_manifest(
        output_dir,
        contract_path=contract_path,
        contract=contract,
        review=None,
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["domain_contract"]["prompt_hash"] == (
        contract.approval.prompt_hash
    )
    assert payload["domain_contract"]["model_hash"] == (
        contract.approval.model_hash
    )


def _schema2_entity(
    entity_type: str,
    type_id: str,
    name: str,
    contract_hash: str,
    evidence_id: str | None,
    *,
    asserted: bool = True,
) -> dict:
    row = _make_entity_row(entity_type, name)
    row.update(
        {
            "evidence_ids": [evidence_id] if evidence_id else [],
            "assertion_state": "asserted" if asserted else "unresolved",
            "semantic_lane": "authoritative",
            "semantic_type_id": type_id,
            "review_status": "approved" if asserted else "needs_review",
            "semantic_contract_hash": contract_hash,
            "properties_json": json.dumps(
                {
                    "semantic_contract_hash": contract_hash,
                    "semantic_lane": "authoritative",
                    "semantic_type_id": type_id,
                    "review_status": "approved" if asserted else "needs_review",
                },
                sort_keys=True,
            ),
        }
    )
    return row


def _schema2_relationship(
    relationship_id: str,
    source_id: str,
    target_id: str,
    contract_hash: str,
    evidence_id: str | None,
    *,
    state: str = "asserted",
) -> dict:
    row = _make_relationship_row(
        source_id,
        target_id,
        rel_type="contains",
        evidence_id=evidence_id,
    )
    row.update(
        {
            "relationship_id": relationship_id,
            "evidence_ids": [evidence_id] if evidence_id else [],
            "semantic_relationship_id": "relationship-type:contains",
            "assertion_state": state,
            "processing_status": (
                "accepted" if state == "asserted" else state
            ),
            "semantic_lane": "authoritative",
            "semantic_contract_hash": contract_hash,
            "reason_codes": (
                ["EVIDENCE_MISSING"] if state == "unresolved" else []
            ),
            "resolved_source_type_id": "entity-type:facility",
            "resolved_target_type_id": "entity-type:equipment",
            "source_inheritance_path": ["entity-type:facility"],
            "target_inheritance_path": ["entity-type:equipment"],
            "validation_authority": "schema2",
            "direction": "forward",
            "review_status": (
                "approved" if state == "asserted" else "needs_review"
            ),
            "properties_json": json.dumps(
                {
                    "semantic_contract_hash": contract_hash,
                    "semantic_lane": "authoritative",
                    "semantic_relationship_id": "relationship-type:contains",
                    "assertion_status": state,
                    "validation_authority": "schema2",
                    "direction": "forward",
                },
                sort_keys=True,
            ),
        }
    )
    return row


def _write_schema2_compile_input(
    tmp_path: Path,
    *,
    unpublished_endpoint: bool = False,
) -> Path:
    input_dir, _ = _write_schema2_authority(tmp_path)
    contract, _ = _load_schema2_projection_authority(input_dir)
    assert contract is not None
    contract_hash = compute_contract_hash(contract)
    evidence = _make_evidence_row()
    evidence.update(
        {
            "runner_verified": True,
            "text_unit_id": "unit:test",
            "span_start": 0,
            "span_end": len(str(evidence["text"])),
            "source_content_hash": content_hash(str(evidence["text"])),
        }
    )
    evidence_id = evidence["evidence_id"]
    facility = _schema2_entity(
        "Facility",
        "entity-type:facility",
        "Building A",
        contract_hash,
        evidence_id,
    )
    equipment = _schema2_entity(
        "Equipment",
        "entity-type:equipment",
        "AHU-4",
        contract_hash,
        evidence_id,
    )
    entities = [facility, equipment]
    target_id = equipment["entity_id"]
    if unpublished_endpoint:
        unpublished = _schema2_entity(
            "Equipment",
            "entity-type:equipment",
            "Unpublished AHU",
            contract_hash,
            None,
            asserted=False,
        )
        entities.append(unpublished)
        target_id = unpublished["entity_id"]
    relationships = [
        _schema2_relationship(
            "relationship:asserted",
            facility["entity_id"],
            target_id,
            contract_hash,
            evidence_id,
        ),
        _schema2_relationship(
            "relationship:unresolved",
            facility["entity_id"],
            equipment["entity_id"],
            contract_hash,
            None,
            state="unresolved",
        ),
    ]
    (input_dir / "batch.json").write_text(
        json.dumps(
            {
                "entities": entities,
                "relationships": relationships,
                "evidence": [evidence],
                "chunks": [],
            }
        ),
        encoding="utf-8",
    )
    return input_dir


def test_compile_data_schema2_writes_reconciled_audit_and_receipt(
    tmp_path: Path,
) -> None:
    input_dir = _write_schema2_compile_input(tmp_path)
    out_dir = tmp_path / "build" / "parquet"
    result = CliRunner().invoke(
        compile_data_cmd,
        ["--input", str(input_dir), "--out", str(out_dir)],
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(
        (out_dir / "semantic-projection-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "succeeded"
    assert receipt["input_candidate_count"] == 2
    assert sum(receipt["terminal_counts"].values()) == 2
    assert pq.read_table(out_dir / "relationships.parquet").num_rows == 2
    assert pq.read_table(out_dir / "semantic_relationships.parquet").num_rows == 1


def test_compile_data_schema2_failure_keeps_receipt_without_serving_output(
    tmp_path: Path,
) -> None:
    input_dir = _write_schema2_compile_input(
        tmp_path,
        unpublished_endpoint=True,
    )
    out_dir = tmp_path / "build" / "parquet"
    result = CliRunner().invoke(
        compile_data_cmd,
        ["--input", str(input_dir), "--out", str(out_dir)],
    )
    assert result.exit_code == 5, result.output
    receipt = json.loads(
        (out_dir / "semantic-projection-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed"
    assert receipt["terminal_counts"]["endpoint_unpublished"] == 1
    assert not (out_dir / "semantic_entities.parquet").exists()
    assert not (out_dir / "semantic_relationships.parquet").exists()


def test_compile_data_write_failure_publishes_only_failed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_schema2_compile_input(tmp_path)
    out_dir = tmp_path / "build" / "parquet"
    out_dir.mkdir(parents=True)
    for name in ("semantic_entities.parquet", "semantic_relationships.parquet"):
        (out_dir / name).write_bytes(b"stale")

    def fail_write(*args, **kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(
        "fabric_kg_builder.cli.compile_data_cmd.write_all_tables",
        fail_write,
    )
    result = CliRunner().invoke(
        compile_data_cmd,
        ["--input", str(input_dir), "--out", str(out_dir)],
    )
    assert result.exit_code != 0
    receipt = json.loads(
        (out_dir / "semantic-projection-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed"
    assert receipt["output_failure"] == {
        "code": "OUTPUT_WRITE_FAILED",
        "error_type": "OSError",
    }
    assert not (out_dir / "semantic_entities.parquet").exists()
    assert not (out_dir / "semantic_relationships.parquet").exists()


def test_schema2_nonasserted_dangling_endpoint_remains_audit_only() -> None:
    rows = {
        "entities": [],
        "relationships": [
            {
                "relationship_id": "relationship:audit",
                "source_entity_id": "unresolved-endpoint:1",
                "target_entity_id": "unresolved-endpoint:2",
                "assertion_state": "unresolved",
                "semantic_contract_hash": "contract:test",
            }
        ],
        "evidence": [],
    }
    assert not {
        violation.gate for violation in run_gates(rows)
    }.intersection({"VAL-005", "VAL-006", "VAL-007"})


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


class TestCompileDataHappyPath:
    def test_exits_zero_with_clean_fixture(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )

    def test_writes_8_parquet_files(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        parquet_files = {p.stem for p in out_dir.glob("*.parquet")}
        # Core canonical tables must always be present.
        required = {
            "entities", "relationships", "chunks", "evidence",
            "source_files", "document_elements", "visual_assets", "visual_regions",
        }
        missing = required - parquet_files
        assert not missing, (
            f"Required Parquet files missing: {missing}\nGot: {parquet_files}"
        )

    def test_entities_parquet_is_readable(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        table = pq.read_table(out_dir / "entities.parquet")
        assert table.num_rows == 2

    def test_relationships_parquet_is_readable(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        table = pq.read_table(out_dir / "relationships.parquet")
        assert table.num_rows == 1

    def test_chunks_parquet_is_readable(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        table = pq.read_table(out_dir / "chunks.parquet")
        assert table.num_rows == 1

    def test_summary_printed_on_success(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert "Summary" in result.output
        assert "entities" in result.output
        assert "relationships" in result.output

    def test_multiple_json_files_merged(self, tmp_path: Path) -> None:
        """Two separate batch files with distinct entities are both written."""
        input_dir = tmp_path / "enriched"
        input_dir.mkdir(parents=True)

        e1 = _make_entity_row("Device", "Surface Laptop 5")
        e2 = _make_entity_row("Device", "Surface Pro 9")
        chunk1 = _make_chunk_row("src:file1")
        chunk2 = _make_chunk_row("src:file2")
        # Give chunk2 a unique content so it gets a different chunk_id
        chunk2["content"] = "Surface Pro 9 repair guide."
        chunk2["content_hash"] = content_hash(chunk2["content"])
        chunk2["chunk_id"] = make_chunk_id("src:file2", "section_text", chunk2["content_hash"])

        (input_dir / "batch1.json").write_text(
            json.dumps({"entities": [e1], "relationships": [], "chunks": [chunk1], "evidence": []}),
            encoding="utf-8",
        )
        (input_dir / "batch2.json").write_text(
            json.dumps({"entities": [e2], "relationships": [], "chunks": [chunk2], "evidence": []}),
            encoding="utf-8",
        )

        out_dir = tmp_path / "parquet"
        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, result.output
        table = pq.read_table(out_dir / "entities.parquet")
        assert table.num_rows == 2


# ---------------------------------------------------------------------------
# Tests: VAL-001 — duplicate entity_id
# ---------------------------------------------------------------------------


class TestValDuplicateEntityId:
    """Duplicate entity_id is resolved by MERGE (canonical entity resolution),
    not treated as a fatal error — the same entity extracted across sections is
    combined into one row. See _resolve_duplicates in compile_data_cmd."""

    def test_exits_zero_merging_duplicates(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _dup_entity_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, (
            f"Expected exit 0 (duplicate entity_id merged), got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

    def test_reports_resolution(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _dup_entity_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert "Resolved duplicates" in result.output, (
            f"Expected duplicate-resolution notice.\nOutput:\n{result.output}"
        )

    def test_merges_to_single_entity_row(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _dup_entity_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, result.output
        table = pq.read_table(out_dir / "entities.parquet")
        eids = table.column("entity_id").to_pylist()
        assert len(eids) == len(set(eids)), (
            f"Expected unique entity_ids after merge, got {eids}"
        )


# ---------------------------------------------------------------------------
# Tests: VAL-005/VAL-006 — dangling relationship FK
# ---------------------------------------------------------------------------


class TestValDanglingRelFk:
    def test_exits_5_on_dangling_source_entity(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _dangling_fk_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 5, (
            f"Expected exit 5 for dangling FK, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

    def test_reports_val006_violation(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _dangling_fk_fixture())
        out_dir = tmp_path / "parquet"

        runner = make_cli_runner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert "VAL-006" in combined_output(result), (
            f"Expected VAL-006 in output.\nOutput:\n{combined_output(result)}"
        )


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestCompileDataEdgeCases:
    def test_missing_input_dir_exits_nonzero(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(tmp_path / "does_not_exist"), "--out", str(tmp_path / "out")],
        )
        assert result.exit_code != 0

    def test_empty_input_dir_exits_zero(self, tmp_path: Path) -> None:
        """Empty enriched dir (no JSON files) should write empty Parquet and exit 0."""
        input_dir = tmp_path / "enriched"
        input_dir.mkdir()
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, (
            f"Empty input dir should exit 0, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

    def test_checkpoint_json_is_skipped(self, tmp_path: Path) -> None:
        """build/enriched/.checkpoint.json must NOT be parsed as a batch file."""
        input_dir = tmp_path / "enriched"
        input_dir.mkdir()
        # Write a .checkpoint.json that looks like an orchestrator checkpoint
        (input_dir / ".checkpoint.json").write_text(
            json.dumps({"completed": ["src:abc"]}), encoding="utf-8"
        )
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, (
            f".checkpoint.json must be skipped.\nOutput:\n{result.output}"
        )

    def test_output_dir_created_if_absent(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "deep" / "nested" / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0
        assert out_dir.exists()
