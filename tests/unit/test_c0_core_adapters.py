"""C0.Core adapters must preserve existing authority fields byte-for-byte."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from fabric_kg_builder.contracts import CanonicalIdentityEnvelope
from fabric_kg_builder.contracts.adapters import (
    assert_domain_hash_authority,
    checkpoint_fingerprint_from_authority,
    entity_assertion_from_row,
    identity_from_common_lineage,
    locator_from_authority,
    semantic_projection_header_ids,
)
from fabric_kg_builder.domain import compute_contract_hash, load_domain_contract
from fabric_kg_builder.lineage.common import build_source_locator
from fabric_kg_builder.model.ids import content_hash, make_id
from fabric_kg_builder.model.schemas import CommonLineageRow, EntityRow
from fabric_kg_builder.sources.checkpoint import compute_checkpoint_fingerprint


@pytest.mark.unit
def test_locator_adapter_equals_build_source_locator() -> None:
    kwargs = {
        "blob_uri": "https://storage.example.test/container/file.txt",
        "blob_version_id": "version-1",
        "page": 2,
        "section_path": "intro/pump",
    }
    adapted = locator_from_authority(**kwargs)
    expected = build_source_locator(**kwargs)
    assert adapted.to_authority() == expected


@pytest.mark.unit
def test_common_lineage_adapter_preserves_fields_and_domain_hash() -> None:
    locator = build_source_locator(
        blob_uri="https://storage.example.test/container/file.txt",
        blob_version_id="version-1",
    )
    row = CommonLineageRow(
        project_id="project-1",
        asset_id="asset-1",
        asset_version_id="asset-version-1",
        run_id="run-1",
        parent_record_id="parent-1",
        source_locator_json=json.dumps(locator),
        schema_version="2.0",
        domain_hash="a" * 64,
    )
    identity = identity_from_common_lineage(
        row,
        contract_kind="c0.source_unit",
        domain_schema_version="2.0",
        canonical_schema_version=row.schema_version,
        content_hash="b" * 64,
        source_file_id="source-file-1",
        source_unit_id="source-unit-1",
    )
    assert identity.project_id == row.project_id
    assert identity.asset_id == row.asset_id
    assert identity.asset_version_id == row.asset_version_id
    assert identity.run_id == row.run_id
    assert identity.parent_record_ids == (row.parent_record_id,)
    assert identity.domain_contract_hash == row.domain_hash


@pytest.mark.unit
def test_domain_hash_adapter_equals_domain_service_authority() -> None:
    contract = load_domain_contract(
        "examples/domains/facility-maintenance-v2.domain.yaml"
    )
    expected = compute_contract_hash(contract)
    assert_domain_hash_authority(contract, expected)
    with pytest.raises(ValueError, match="domain_contract_hash"):
        assert_domain_hash_authority(contract, "0" * 64)


@pytest.mark.unit
def test_checkpoint_adapter_equals_existing_checkpoint_seed() -> None:
    kwargs = {
        "content_hash": "a" * 64,
        "adapter_name": "fixture-adapter",
        "adapter_version": "1.0.0",
        "options": {"mode": "exact", "max_rows": 10},
    }
    assert checkpoint_fingerprint_from_authority(**kwargs) == (
        compute_checkpoint_fingerprint(**kwargs)
    )


@pytest.mark.unit
def test_contract_id_seed_is_compatible_with_make_id() -> None:
    from fabric_kg_builder.contracts.base import (
        canonical_json,
        deterministic_contract_id,
    )

    seed = {"z": "Cafe\u0301", "a": 1}
    assert deterministic_contract_id("candidate", seed) == make_id(
        "candidate",
        canonical_json(seed),
    )


@pytest.mark.unit
def test_entity_adapter_preserves_canonical_id_key_and_hash() -> None:
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    row = EntityRow(
        entity_id="entity:1",
        entity_type="Equipment",
        display_name="Pump A",
        canonical_key="equipment:pump-a",
        aliases=["Pump A"],
        project_id="project-1",
        asset_id="asset-1",
        asset_version_id="asset-version-1",
        run_id="run-1",
        source_file_id="source-file-1",
        domain_hash="b" * 64,
        content_hash=content_hash("equipment:pump-a"),
        created_at=now,
        updated_at=now,
    )
    identity = CanonicalIdentityEnvelope(
        contract_kind="c0.canonical_entity_assertion",
        contract_version="1.0.0",
        project_id="project-1",
        asset_id="asset-1",
        asset_version_id="asset-version-1",
        run_id="run-1",
        source_file_id="source-file-1",
        source_unit_id=None,
        content_hash=row.content_hash,
        domain_schema_version="2.0",
        domain_contract_hash="b" * 64,
        semantic_contract_hash="c" * 64,
        canonical_schema_version="2.0",
        prompt_version=None,
        prompt_hash=None,
        model_version=None,
        model_hash=None,
        extractor_name=None,
        extractor_version=None,
        parent_artifact_ids=(),
        parent_record_ids=(),
        immutable_locator=None,
    )
    assertion = entity_assertion_from_row(
        row,
        identity=identity,
        semantic_type_id="entity-type:equipment",
        evidence_span_ids=("evidence-span-1",),
        lifecycle_record_id="lifecycle-1",
        assertion_state="asserted",
    )
    assert assertion.entity_id == row.entity_id
    assert assertion.canonical_key == row.canonical_key
    assert assertion.content_hash == row.content_hash
    unrelated = identity.model_copy(update={"run_id": "different-run"})
    with pytest.raises(ValueError, match="run_id"):
        entity_assertion_from_row(
            row,
            identity=unrelated,
            semantic_type_id="entity-type:equipment",
            evidence_span_ids=("evidence-span-1",),
            lifecycle_record_id="lifecycle-1",
            assertion_state="asserted",
        )
    row_with_locator = row.model_copy(
        update={
            "source_locator_json": json.dumps(
                build_source_locator(
                    blob_uri="https://storage.example.test/container/file.txt",
                    blob_version_id="version-1",
                )
            )
        }
    )
    with pytest.raises(ValueError, match="source locator"):
        entity_assertion_from_row(
            row_with_locator,
            identity=identity,
            semantic_type_id="entity-type:equipment",
            evidence_span_ids=("evidence-span-1",),
            lifecycle_record_id="lifecycle-1",
            assertion_state="asserted",
        )


@pytest.mark.unit
def test_semantic_header_adapter_preserves_existing_projection_ids() -> None:
    projection = {
        "semantic_entities": [
            {"entity_id": "entity-2"},
            {"entity_id": "entity-1"},
        ],
        "semantic_relationships": [
            {
                "relationship_id": "relationship-2",
                "assertion_status": "unverified",
            },
            {
                "relationship_id": "relationship-1",
                "assertion_status": "asserted",
            },
        ],
    }
    assert semantic_projection_header_ids(projection) == (
        ("entity-1", "entity-2"),
        ("relationship-1",),
    )
