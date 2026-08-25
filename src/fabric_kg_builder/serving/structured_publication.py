"""Schema-2 L5a structured publication over one sealed L4 serving source."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from fabric_kg_builder.contracts.base import (
    canonical_json,
    canonical_sha256,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.extraction import RequiredMemberManifestV1_1
from fabric_kg_builder.contracts.identity import (
    CanonicalIdentityEnvelope,
    ImmutableSourceLocator,
)
from fabric_kg_builder.contracts.publication import (
    AccessPolicy,
    GovernedAssetReference,
    ProjectionEquivalence,
    ProjectionEvidence,
    PublicationCrosswalk,
    StorageReference,
)
from fabric_kg_builder.contracts.receipts import (
    ArtifactEntry,
    ArtifactManifest,
    StageReceipt,
)
from fabric_kg_builder.contracts.resources import (
    StageResourceMetrics,
    validate_receipt_resources,
)
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource

L5A_STAGE_NAME = "schema2-structured-publication"
L5A_STAGE_CONTRACT_VERSION = "1.0.0"
L5A_PUBLICATION_CODE_VERSION = "0.2.3/l5a-2"
L5A_STATE_DIR = Path(".fkg") / "l5a"
L5A_TARGET_VERSION = "1.0.0"
L5A_TARGET_ORDER = ("parquet", "semantic_model", "ontology", "graph")
L5ATargetKind = Literal["parquet", "semantic_model", "ontology", "graph"]
L5A_TARGET_COUNT = len(L5A_TARGET_ORDER)
L5A_REUSE_READ_BACK_CALLS = L5A_TARGET_COUNT
L5A_INSPECT_CALLS = L5A_TARGET_COUNT
L5A_PUBLISH_CALLS = L5A_TARGET_COUNT
L5A_POST_PUBLISH_READ_BACK_CALLS = L5A_TARGET_COUNT
L5A_ROLLBACK_MUTATION_CALLS = L5A_TARGET_COUNT
L5A_AMBIGUOUS_RECOVERY_INSPECT_CALLS = 1
L5A_MAX_ROLLBACK_PHASE_CALLS = (
    L5A_ROLLBACK_MUTATION_CALLS
    + L5A_AMBIGUOUS_RECOVERY_INSPECT_CALLS
)
L5A_MAX_SUCCESS_FABRIC_CALLS = (
    L5A_REUSE_READ_BACK_CALLS
    + L5A_INSPECT_CALLS
    + L5A_PUBLISH_CALLS
    + L5A_POST_PUBLISH_READ_BACK_CALLS
)
L5A_POST_READ_BACK_FAILURE_MAX_CALLS = (
    L5A_MAX_SUCCESS_FABRIC_CALLS
    + L5A_ROLLBACK_MUTATION_CALLS
)
L5A_AMBIGUOUS_PUBLISH_FAILURE_MAX_CALLS = (
    L5A_REUSE_READ_BACK_CALLS
    + L5A_INSPECT_CALLS
    + L5A_PUBLISH_CALLS
    + L5A_MAX_ROLLBACK_PHASE_CALLS
)
L5A_MAX_FABRIC_CALLS = max(
    L5A_POST_READ_BACK_FAILURE_MAX_CALLS,
    L5A_AMBIGUOUS_PUBLISH_FAILURE_MAX_CALLS,
)

L5A_ACCEPTED_VERSIONS = {
    "c0.access_policy": "1.0.0",
    "c0.artifact_manifest": "1.0.0",
    "c0.governed_asset_reference": "1.0.0",
    "c0.projection_equivalence": "1.0.0",
    "c0.publication_crosswalk": "1.0.0",
    "c0.required_member_manifest": "1.1.0",
    "c0.semantic_serving_projection": "1.0.0",
    "c0.stage_receipt": "1.0.0",
    "c0.stage_resource_metrics": "1.0.0",
}

_SOURCE_TABLES = (
    "semantic_asserted_entities",
    "semantic_entity_type_assertions",
    "semantic_asserted_relationships",
    "semantic_asserted_properties",
    "semantic_required_member_manifests",
    "semantic_required_members",
)
_FIXED_FILES = {
    Path("access-policy.json"),
    Path("governed-assets.json"),
    Path("publication-crosswalks.json"),
    Path("projection-equivalence.json"),
    Path("output-manifest.json"),
    Path("resource-metrics.json"),
    Path("stage-receipt.json"),
}


def _budget_snapshot_hash() -> str:
    return canonical_sha256({
        "stage": "L5a",
        "bounded_target_calls": L5A_MAX_FABRIC_CALLS,
        "state_machine": {
            "reuse_read_back": L5A_REUSE_READ_BACK_CALLS,
            "inspect": L5A_INSPECT_CALLS,
            "publish": L5A_PUBLISH_CALLS,
            "post_publish_read_back": L5A_POST_PUBLISH_READ_BACK_CALLS,
            "rollback": {
                "mutations": L5A_ROLLBACK_MUTATION_CALLS,
                "ambiguous_recovery_inspect": (
                    L5A_AMBIGUOUS_RECOVERY_INSPECT_CALLS
                ),
                "phase_max": L5A_MAX_ROLLBACK_PHASE_CALLS,
                "mutually_exclusive_path_max": max(
                    L5A_POST_READ_BACK_FAILURE_MAX_CALLS,
                    L5A_AMBIGUOUS_PUBLISH_FAILURE_MAX_CALLS,
                ),
            },
        },
        "numeric_thresholds": None,
    })


class L5aPublicationError(RuntimeError):
    """Fail-closed L5a error with optional failed receipt evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        receipt: StageReceipt | None = None,
        metrics: StageResourceMetrics | None = None,
    ) -> None:
        self.code = code
        self.receipt = receipt
        self.metrics = metrics
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class L5aTableSnapshot:
    table_id: str
    schema_hash: str
    row_count: int
    canonical_id_set_hash: str
    row_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "schema_hash": self.schema_hash,
            "row_count": self.row_count,
            "canonical_id_set_hash": self.canonical_id_set_hash,
            "row_fingerprint": self.row_fingerprint,
        }


@dataclass(frozen=True)
class L5aRequiredMemberSnapshot:
    required_member_manifest_id: str
    required_member_manifest_schema_hash: str
    required_member_manifest_hash: str
    authoritative_collection_hash: str
    source_artifact_manifest_id: str
    source_artifact_manifest_hash: str
    canonical_ids: tuple[str, ...]
    row_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "required_member_manifest_id": self.required_member_manifest_id,
            "required_member_manifest_schema_hash": (
                self.required_member_manifest_schema_hash
            ),
            "required_member_manifest_hash": self.required_member_manifest_hash,
            "authoritative_collection_hash": self.authoritative_collection_hash,
            "source_artifact_manifest_id": self.source_artifact_manifest_id,
            "source_artifact_manifest_hash": self.source_artifact_manifest_hash,
            "canonical_ids": list(self.canonical_ids),
            "row_fingerprint": self.row_fingerprint,
        }


@dataclass(frozen=True)
class L5aRemoteAccounting:
    operation_refs: tuple[str, ...]
    request_bytes: int
    response_bytes: int
    retry_count: int
    retry_wait_ms: int
    error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class L5aStateOperation:
    state: L5aTargetState | None
    accounting: L5aRemoteAccounting


@dataclass(frozen=True)
class L5aTargetState:
    target_kind: L5ATargetKind
    target_id: str
    target_version: str
    definition: Mapping[str, Any]
    table_snapshots: tuple[L5aTableSnapshot, ...]
    access_policy_id: str
    access_policy_hash: str
    publication_token: str
    required_member_manifest_rows: tuple[Mapping[str, Any], ...]
    required_member_rows: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if not self.publication_token:
            raise ValueError("publication_token must be non-empty")


@dataclass(frozen=True)
class L5aPublishOperation:
    target_kind: L5ATargetKind
    target_id: str
    created: bool
    publication_token: str
    applied: bool
    accounting: L5aRemoteAccounting

    def __post_init__(self) -> None:
        if not self.publication_token:
            raise ValueError("publication_token must be non-empty")


class L5aTargetClient(Protocol):
    """Bounded target adapter; live Fabric implementations belong to L7."""

    def inspect(
        self,
        target_kind: L5ATargetKind,
        target_id: str,
    ) -> L5aStateOperation:
        ...

    def publish(
        self,
        target_kind: L5ATargetKind,
        target_id: str,
        *,
        definition_path: Path,
        table_paths: Mapping[str, Path],
        access_policy: AccessPolicy,
        expected_state: L5aTargetState | None,
        publication_token: str,
    ) -> L5aPublishOperation:
        ...

    def read_back(
        self,
        target_kind: L5ATargetKind,
        target_id: str,
    ) -> L5aStateOperation:
        ...

    def cleanup(
        self,
        target_kind: L5ATargetKind,
        target_id: str,
        *,
        publication_token: str,
    ) -> L5aPublishOperation:
        """Atomically delete only when the persisted token still matches."""
        ...

    def restore(
        self,
        target_kind: L5ATargetKind,
        target_id: str,
        *,
        prior_state: L5aTargetState,
        publication_token: str,
    ) -> L5aPublishOperation:
        """Atomically restore prior state only when the current token matches."""
        ...


@dataclass(frozen=True)
class L5aCompiledPublication:
    source: SealedL4ServingSource
    fingerprint: str
    crosswalks: tuple[PublicationCrosswalk, ...]
    access_policy: AccessPolicy
    governed_assets: tuple[GovernedAssetReference, ...]
    target_ids: Mapping[L5ATargetKind, str]
    definitions: Mapping[L5ATargetKind, Mapping[str, Any]]
    tables: Mapping[str, pa.Table]
    table_snapshots: tuple[L5aTableSnapshot, ...]
    required_member_manifest_rows: tuple[Mapping[str, Any], ...]
    required_member_rows: tuple[Mapping[str, Any], ...]
    required_member_snapshots: tuple[L5aRequiredMemberSnapshot, ...]


@dataclass(frozen=True)
class L5aStageResult:
    compiled: L5aCompiledPublication
    projection_equivalences: tuple[ProjectionEquivalence, ...]
    output_manifest: ArtifactManifest
    metrics: StageResourceMetrics
    receipt: StageReceipt
    run_root: Path
    reused: bool


@dataclass
class _CallAccounting:
    fabric_calls: int = 0
    network_request_bytes: int = 0
    network_response_bytes: int = 0
    retry_count: int = 0
    retry_wait_ms: int = 0
    remote_operation_refs: tuple[str, ...] = ()
    remote_error_codes: tuple[str, ...] = ()
    exceeded_dimensions: tuple[str, ...] = ()

    def begin_call(self) -> None:
        if self.fabric_calls >= L5A_MAX_FABRIC_CALLS:
            self.exceeded_dimensions = tuple(sorted({
                *self.exceeded_dimensions,
                "fabric_calls",
            }))
            raise L5aPublicationError(
                "L5A_CALL_BUDGET_EXCEEDED",
                f"remote call budget {L5A_MAX_FABRIC_CALLS} exhausted",
            )
        self.fabric_calls += 1

    def observe(self, remote: L5aRemoteAccounting) -> None:
        remote = _validate_remote_accounting(remote)
        overlap = set(self.remote_operation_refs).intersection(
            remote.operation_refs
        )
        if overlap:
            self.record_error("L5A_REMOTE_REFERENCE_REUSED")
            raise L5aPublicationError(
                "L5A_REMOTE_REFERENCE_REUSED",
                f"remote operation references were reused: {sorted(overlap)}",
            )
        self.network_request_bytes += remote.request_bytes
        self.network_response_bytes += remote.response_bytes
        self.retry_count += remote.retry_count
        self.retry_wait_ms += remote.retry_wait_ms
        self.remote_operation_refs = tuple(sorted({
            *self.remote_operation_refs,
            *remote.operation_refs,
        }))
        self.remote_error_codes = tuple(sorted({
            *self.remote_error_codes,
            *remote.error_codes,
        }))
        if remote.error_codes:
            raise L5aPublicationError(
                "L5A_REMOTE_OPERATION_FAILED",
                f"remote operation reported errors {remote.error_codes}",
            )

    def record_error(self, code: str) -> None:
        self.remote_error_codes = tuple(sorted({
            *self.remote_error_codes,
            code,
        }))

    def require_complete_references(self) -> None:
        remote_refs = [
            value
            for value in self.remote_operation_refs
            if not value.startswith("publication-token:")
        ]
        if len(remote_refs) != self.fabric_calls:
            self.record_error("L5A_REMOTE_REFERENCE_COUNT_MISMATCH")
            raise L5aPublicationError(
                "L5A_REMOTE_REFERENCE_COUNT_MISMATCH",
                f"{self.fabric_calls} calls produced {len(remote_refs)} unique "
                "remote operation references",
            )


def _validate_remote_accounting(value: object) -> L5aRemoteAccounting:
    if not isinstance(value, L5aRemoteAccounting):
        raise L5aPublicationError(
            "L5A_REMOTE_ACCOUNTING_MISSING",
            "target adapter omitted uniform remote accounting",
        )
    for name in (
        "request_bytes",
        "response_bytes",
        "retry_count",
        "retry_wait_ms",
    ):
        counter = getattr(value, name)
        if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
            raise L5aPublicationError(
                "L5A_REMOTE_ACCOUNTING_INVALID",
                f"remote accounting {name} must be a nonnegative integer",
            )
    if (
        not isinstance(value.operation_refs, tuple)
        or not isinstance(value.error_codes, tuple)
        or not value.operation_refs
        or value.request_bytes <= 0
        or value.response_bytes <= 0
        or any(
            not isinstance(item, str) or not item
            for item in (*value.operation_refs, *value.error_codes)
        )
        or value.operation_refs != tuple(sorted(set(value.operation_refs)))
        or value.error_codes != tuple(sorted(set(value.error_codes)))
    ):
        raise L5aPublicationError(
            "L5A_REMOTE_ACCOUNTING_INVALID",
            "remote accounting contains malformed references or error codes",
        )
    return value


def _validate_state_operation(operation: object) -> L5aStateOperation:
    if not isinstance(operation, L5aStateOperation):
        raise L5aPublicationError(
            "L5A_REMOTE_ACCOUNTING_MISSING",
            "state operation omitted uniform accounting",
        )
    if operation.state is not None and not isinstance(
        operation.state,
        L5aTargetState,
    ):
        raise L5aPublicationError(
            "L5A_REMOTE_STATE_INVALID",
            "state operation returned an unsupported target state",
        )
    return operation


def _validate_operation(operation: object) -> L5aPublishOperation:
    if not isinstance(operation, L5aPublishOperation):
        raise L5aPublicationError(
            "L5A_REMOTE_ACCOUNTING_MISSING",
            "target mutation omitted uniform accounting",
        )
    if (
        not isinstance(operation.created, bool)
        or not isinstance(operation.applied, bool)
        or not operation.publication_token
    ):
        raise L5aPublicationError(
            "L5A_OPERATION_INVALID",
            "target operation contains malformed status fields",
        )
    return operation


def _invoke_state_operation(
    accounting: _CallAccounting,
    operation_name: str,
    callback: Any,
) -> L5aTargetState | None:
    accounting.begin_call()
    try:
        raw_operation = callback()
    except Exception as exc:
        accounting.record_error("L5A_REMOTE_ACCOUNTING_MISSING")
        raise L5aPublicationError(
            "L5A_REMOTE_ACCOUNTING_MISSING",
            f"{operation_name} failed without accounting metadata: {exc}",
        ) from exc
    if not isinstance(raw_operation, L5aStateOperation):
        accounting.record_error("L5A_REMOTE_ACCOUNTING_MISSING")
        raise L5aPublicationError(
            "L5A_REMOTE_ACCOUNTING_MISSING",
            f"{operation_name} returned no state accounting envelope",
        )
    try:
        remote_accounting = _validate_remote_accounting(
            raw_operation.accounting
        )
    except L5aPublicationError as exc:
        accounting.record_error(exc.code)
        raise
    accounting.observe(remote_accounting)
    try:
        operation = _validate_state_operation(raw_operation)
    except L5aPublicationError as exc:
        accounting.record_error(exc.code)
        raise
    return operation.state


def _invoke_mutation_operation(
    accounting: _CallAccounting,
    operation_name: str,
    callback: Any,
) -> L5aPublishOperation:
    accounting.begin_call()
    try:
        raw_operation = callback()
    except Exception as exc:
        accounting.record_error("L5A_REMOTE_ACCOUNTING_MISSING")
        raise L5aPublicationError(
            "L5A_REMOTE_ACCOUNTING_MISSING",
            f"{operation_name} failed without accounting metadata: {exc}",
        ) from exc
    if not isinstance(raw_operation, L5aPublishOperation):
        accounting.record_error("L5A_REMOTE_ACCOUNTING_MISSING")
        raise L5aPublicationError(
            "L5A_REMOTE_ACCOUNTING_MISSING",
            f"{operation_name} returned no mutation accounting envelope",
        )
    try:
        remote_accounting = _validate_remote_accounting(
            raw_operation.accounting
        )
    except L5aPublicationError as exc:
        accounting.record_error(exc.code)
        raise
    accounting.observe(remote_accounting)
    try:
        operation = _validate_operation(raw_operation)
    except L5aPublicationError as exc:
        accounting.record_error(exc.code)
        raise
    return operation


def _identity(
    source: SealedL4ServingSource,
    *,
    contract_kind: str,
) -> CanonicalIdentityEnvelope:
    values = source.receipt.identity.model_dump(mode="python", round_trip=True)
    values.update({
        "contract_kind": contract_kind,
        "canonical_schema_version": "2.0",
        "parent_artifact_ids": tuple(sorted({
            *source.receipt.identity.parent_artifact_ids,
            source.manifest.artifact_manifest_id,
            source.input_manifest.artifact_manifest_id,
        })),
    })
    return CanonicalIdentityEnvelope.model_validate(values)


def _identity_lineage(value: CanonicalIdentityEnvelope) -> dict[str, Any]:
    return value.model_dump(mode="json", exclude={"contract_kind"})


def _schema_descriptor(schema: pa.Schema) -> list[dict[str, Any]]:
    return [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in schema
    ]


def _table_snapshot(table_id: str, table: pa.Table) -> L5aTableSnapshot:
    id_column = "__canonical_id" if "__canonical_id" in table.column_names else None
    if id_column is None:
        source_id_field = {
            "l4_semantic_asserted_entities": "entity_id",
            "l4_semantic_asserted_relationships": "relationship_id",
            "l4_semantic_asserted_properties": "property_assertion_id",
        }.get(table_id)
        if source_id_field is not None:
            ids = [str(value) for value in table[source_id_field].to_pylist()]
        elif table_id == "l4_semantic_entity_type_assertions":
            ids = [
                f"{entity_id}|{type_id}"
                for entity_id, type_id in zip(
                    table["entity_id"].to_pylist(),
                    table["semantic_type_id"].to_pylist(),
                    strict=True,
                )
            ]
        elif "required_member_manifest_id" not in table.column_names:
            raise L5aPublicationError(
                "L5A_TABLE_IDENTITY_MISSING",
                f"table {table_id!r} has no canonical identity column",
            )
        elif "member_canonical_id" in table.column_names:
            ids = [
                f"{manifest_id}|{member_id}"
                for manifest_id, member_id in zip(
                    table["required_member_manifest_id"].to_pylist(),
                    table["member_canonical_id"].to_pylist(),
                    strict=True,
                )
            ]
        else:
            ids = [
                str(value)
                for value in table["required_member_manifest_id"].to_pylist()
            ]
    else:
        ids = [str(value) for value in table[id_column].to_pylist()]
    rows = table.to_pylist()
    return L5aTableSnapshot(
        table_id=table_id,
        schema_hash=canonical_sha256(_schema_descriptor(table.schema)),
        row_count=table.num_rows,
        canonical_id_set_hash=canonical_sha256(sorted(ids)),
        row_fingerprint=canonical_sha256(rows),
    )


def _load_source_tables(
    source: SealedL4ServingSource,
) -> dict[str, pa.Table]:
    sealed = SealedL4ServingSource(
        root=source.root,
        projection=source.projection,
        receipt=source.receipt,
        manifest=source.manifest,
        input_manifest=source.input_manifest,
    )
    return {
        table_name: pq.read_table(sealed.resolve(table_name))
        for table_name in _SOURCE_TABLES
    }


def _single_hash(
    rows: Sequence[Mapping[str, Any]],
    field: str,
    *,
    code: str,
) -> str:
    values = {str(row[field]) for row in rows}
    if len(values) != 1:
        raise L5aPublicationError(code, f"{field} must have one sealed value")
    return next(iter(values))


def _validate_publish_authority(
    source: SealedL4ServingSource,
    tables: Mapping[str, pa.Table],
    crosswalks: Sequence[PublicationCrosswalk],
    access_policy: AccessPolicy,
) -> None:
    manifest_rows = tables["semantic_required_member_manifests"].to_pylist()
    if not manifest_rows:
        raise L5aPublicationError(
            "L5A_REQUIRED_MEMBER_AUTHORITY_MISSING",
            "L5a requires at least one sealed RequiredMemberManifestV1_1",
        )
    manifest_by_id = {
        str(row["required_member_manifest_id"]): row for row in manifest_rows
    }
    if len(manifest_by_id) != len(manifest_rows):
        raise L5aPublicationError(
            "L5A_REQUIRED_MEMBER_AUTHORITY_DUPLICATE",
            "required-member manifest IDs are not unique",
        )
    crosswalk_by_manifest = {
        item.authority.required_member_manifest_id: item for item in crosswalks
    }
    if (
        len(crosswalk_by_manifest) != len(crosswalks)
        or set(crosswalk_by_manifest) != set(manifest_by_id)
    ):
        raise L5aPublicationError(
            "L5A_PUBLICATION_CROSSWALK_SET_MISMATCH",
            "crosswalk authorities must exactly cover sealed required-member manifests",
        )

    source_entries = {
        entry.artifact_id: entry
        for entry in source.input_manifest.entries
        if entry.contract_kind == "c0.required_member_manifest"
    }
    schema_hash = canonical_sha256(RequiredMemberManifestV1_1.model_json_schema())
    if set(source_entries) != set(manifest_by_id):
        raise L5aPublicationError(
            "L5A_L3_AUTHORITY_MISMATCH",
            "anchored L3 RequiredMemberManifest entries differ from L4",
        )

    entity_rows = tables["semantic_asserted_entities"].to_pylist()
    type_rows = tables["semantic_entity_type_assertions"].to_pylist()
    relationship_rows = tables["semantic_asserted_relationships"].to_pylist()
    property_rows = tables["semantic_asserted_properties"].to_pylist()
    hierarchy_hash = _single_hash(
        [*entity_rows, *type_rows, *relationship_rows, *manifest_rows],
        "hierarchy_hash",
        code="L5A_HIERARCHY_AUTHORITY_MISMATCH",
    )
    identity_policy_hash = _single_hash(
        [*entity_rows, *type_rows, *manifest_rows],
        "identity_policy_hash",
        code="L5A_IDENTITY_POLICY_AUTHORITY_MISMATCH",
    )
    member_rows = tables["semantic_required_members"].to_pylist()
    observed_type_ids = {
        str(row["semantic_type_id"]) for row in type_rows
    } | {
        str(row["member_semantic_type_id"]) for row in member_rows
    }
    observed_relationship_ids = {
        str(row["semantic_relationship_id"]) for row in relationship_rows
    } | {
        str(row["membership_semantic_relationship_id"])
        for row in manifest_rows
    }
    observed_property_ids = {
        str(row["semantic_property_id"]) for row in property_rows
    }
    if property_rows:
        raise L5aPublicationError(
            "L5A_PROPERTY_MATERIALIZATION_UNSUPPORTED",
            "sealed L4 property assertions do not carry owner/value fields and "
            "cannot be materialized without invention",
        )

    expected_lineage = _identity_lineage(source.receipt.identity)
    for crosswalk in crosswalks:
        authority = crosswalk.authority
        manifest_row = manifest_by_id[authority.required_member_manifest_id]
        entry = source_entries[authority.required_member_manifest_id]
        if (
            authority.required_member_manifest_contract_version != "1.1.0"
            or authority.required_member_manifest_schema_hash != schema_hash
            or authority.required_member_manifest_hash != manifest_row["manifest_hash"]
            or authority.authoritative_collection_hash
            != manifest_row["authoritative_collection_hash"]
            or authority.source_artifact_manifest_id
            != source.input_manifest.artifact_manifest_id
            or authority.source_artifact_manifest_hash
            != source.input_manifest.manifest_hash
            or entry.contract_version != "1.1.0"
            or entry.schema_hash != schema_hash
            or entry.content_hash != manifest_row["manifest_hash"]
            or entry.canonical_id_set_hash != manifest_row["member_set_hash"]
        ):
            raise L5aPublicationError(
                "L5A_PUBLICATION_AUTHORITY_MISMATCH",
                f"authority tuple differs for {authority.required_member_manifest_id}",
            )
        if (
            crosswalk.source_projection_id != source.projection.projection_id
            or crosswalk.source_projection_hash != source.projection.projection_hash
            or crosswalk.semantic_contract_hash
            != source.projection.sealed_semantic_contract_hash
            or crosswalk.hierarchy_hash != hierarchy_hash
            or crosswalk.identity_policy_hash != identity_policy_hash
            or _identity_lineage(crosswalk.identity) != expected_lineage
        ):
            raise L5aPublicationError(
                "L5A_PUBLICATION_CROSSWALK_STALE",
                f"crosswalk {crosswalk.publication_crosswalk_id} is stale",
            )
        mapped_type_ids = {
            item.canonical_semantic_type_id
            for item in crosswalk.semantic_type_mappings
        }
        mapped_relationship_ids = {
            item.canonical_semantic_relationship_id
            for item in crosswalk.relationship_mappings
        }
        mapped_property_ids = {
            prop.canonical_property_id
            for item in crosswalk.semantic_type_mappings
            for prop in item.property_mappings
        }
        if mapped_type_ids != observed_type_ids:
            raise L5aPublicationError(
                "L5A_TYPE_MAPPING_SET_MISMATCH",
                "crosswalk type IDs differ from sealed L4 type assertions",
            )
        if mapped_relationship_ids != observed_relationship_ids:
            raise L5aPublicationError(
                "L5A_RELATIONSHIP_MAPPING_SET_MISMATCH",
                "crosswalk relationship IDs differ from sealed L4 relationships",
            )
        if not observed_property_ids.issubset(mapped_property_ids):
            raise L5aPublicationError(
                "L5A_PROPERTY_MAPPING_SET_MISMATCH",
                "crosswalk omits a sealed L4 property assertion",
            )
        reserved_entity_columns = {
            "__canonical_id",
            "__semantic_type_id",
            "__most_specific_type_id",
            "__hierarchy_depth",
        }
        reserved_relationship_columns = {
            "__canonical_id",
            "__semantic_relationship_id",
            "__source_entity_id",
            "__target_entity_id",
        }
        if any(
            prop.physical_column_id in reserved_entity_columns
            for item in crosswalk.semantic_type_mappings
            for prop in item.property_mappings
        ) or any(
            field.physical_column_id in reserved_relationship_columns
            for item in crosswalk.relationship_mappings
            for field in (*item.source_key_fields, *item.target_key_fields)
        ):
            raise L5aPublicationError(
                "L5A_RESERVED_COLUMN_COLLISION",
                "crosswalk physical columns collide with L5a identity columns",
            )
        type_ids = {item.canonical_semantic_type_id for item in crosswalk.semantic_type_mappings}
        for relationship in crosswalk.relationship_mappings:
            if (
                relationship.source_semantic_type_id not in type_ids
                or relationship.target_semantic_type_id not in type_ids
            ):
                raise L5aPublicationError(
                    "L5A_RELATIONSHIP_ENDPOINT_MISMATCH",
                    f"relationship {relationship.canonical_semantic_relationship_id} "
                    "has an unknown endpoint type",
                )
            for row in relationship_rows:
                if (
                    row["semantic_relationship_id"]
                    != relationship.canonical_semantic_relationship_id
                ):
                    continue
                if (
                    relationship.source_semantic_type_id
                    not in row["source_inheritance_path"]
                    or relationship.target_semantic_type_id
                    not in row["target_inheritance_path"]
                ):
                    raise L5aPublicationError(
                        "L5A_RELATIONSHIP_ENDPOINT_MISMATCH",
                        f"relationship {row['relationship_id']} endpoint paths "
                        "differ from the crosswalk",
                    )

    if _identity_lineage(access_policy.identity) != expected_lineage:
        raise L5aPublicationError(
            "L5A_ACCESS_POLICY_AUTHORITY_MISMATCH",
            "access policy identity differs from the sealed L4 source",
        )
def _canonical_crosswalk(
    crosswalks: Sequence[PublicationCrosswalk],
) -> PublicationCrosswalk:
    first = crosswalks[0]
    for item in crosswalks[1:]:
        if (
            item.semantic_type_mappings != first.semantic_type_mappings
            or item.relationship_mappings != first.relationship_mappings
            or item.semantic_contract_hash != first.semantic_contract_hash
            or item.stable_id_lock_id != first.stable_id_lock_id
            or item.stable_id_lock_hash != first.stable_id_lock_hash
            or item.hierarchy_hash != first.hierarchy_hash
            or item.identity_policy_hash != first.identity_policy_hash
            or item.source_projection_id != first.source_projection_id
            or item.source_projection_hash != first.source_projection_hash
        ):
            raise L5aPublicationError(
                "L5A_CROSSWALK_DEFINITION_DRIFT",
                "all per-manifest crosswalks must carry one physical definition",
            )
    return first


def _entity_tables(
    source_tables: Mapping[str, pa.Table],
    crosswalk: PublicationCrosswalk,
) -> dict[str, pa.Table]:
    entities = {
        str(row["entity_id"]): row
        for row in source_tables["semantic_asserted_entities"].to_pylist()
    }
    assertions_by_type: dict[str, list[dict[str, Any]]] = {}
    for row in source_tables["semantic_entity_type_assertions"].to_pylist():
        assertions_by_type.setdefault(str(row["semantic_type_id"]), []).append(row)
    result: dict[str, pa.Table] = {}
    for mapping in crosswalk.semantic_type_mappings:
        fields = [
            pa.field("__canonical_id", pa.string(), nullable=False),
            pa.field("__semantic_type_id", pa.string(), nullable=False),
            pa.field("__most_specific_type_id", pa.string(), nullable=False),
            pa.field("__hierarchy_depth", pa.int32(), nullable=False),
        ]
        for prop in mapping.property_mappings:
            fields.append(pa.field(
                prop.physical_column_id,
                pa.string(),
                nullable=True,
            ))
        rows: list[dict[str, Any]] = []
        for assertion in sorted(
            assertions_by_type.get(mapping.canonical_semantic_type_id, []),
            key=lambda item: str(item["entity_id"]),
        ):
            entity_id = str(assertion["entity_id"])
            entity = entities.get(entity_id)
            if entity is None:
                raise L5aPublicationError(
                    "L5A_ENTITY_TYPE_ORPHAN",
                    f"type assertion references missing entity {entity_id}",
                )
            row: dict[str, Any] = {
                "__canonical_id": entity_id,
                "__semantic_type_id": mapping.canonical_semantic_type_id,
                "__most_specific_type_id": entity["most_specific_type_id"],
                "__hierarchy_depth": assertion["hierarchy_depth"],
            }
            for prop in mapping.property_mappings:
                row[prop.physical_column_id] = None
            rows.append(row)
        result[mapping.physical_table_id] = pa.Table.from_pylist(
            rows,
            schema=pa.schema(fields),
        )
    return result


def _relationship_tables(
    source_tables: Mapping[str, pa.Table],
    crosswalk: PublicationCrosswalk,
) -> dict[str, pa.Table]:
    rows_by_type: dict[str, list[dict[str, Any]]] = {}
    for row in source_tables["semantic_asserted_relationships"].to_pylist():
        rows_by_type.setdefault(
            str(row["semantic_relationship_id"]),
            [],
        ).append(row)
    result: dict[str, pa.Table] = {}
    for mapping in crosswalk.relationship_mappings:
        fields = [
            pa.field("__canonical_id", pa.string(), nullable=False),
            pa.field("__semantic_relationship_id", pa.string(), nullable=False),
            pa.field("__source_entity_id", pa.string(), nullable=False),
            pa.field("__target_entity_id", pa.string(), nullable=False),
        ]
        for key in (*mapping.source_key_fields, *mapping.target_key_fields):
            fields.append(pa.field(key.physical_column_id, pa.string(), nullable=True))
        table_rows = []
        for relationship in sorted(
            rows_by_type.get(mapping.canonical_semantic_relationship_id, []),
            key=lambda item: str(item["relationship_id"]),
        ):
            row = {
                "__canonical_id": relationship["relationship_id"],
                "__semantic_relationship_id": mapping.canonical_semantic_relationship_id,
                "__source_entity_id": relationship["source_entity_id"],
                "__target_entity_id": relationship["target_entity_id"],
            }
            row.update({
                key.physical_column_id: None
                for key in mapping.source_key_fields
            })
            row.update({
                key.physical_column_id: None
                for key in mapping.target_key_fields
            })
            table_rows.append(row)
        result[mapping.physical_table_id] = pa.Table.from_pylist(
            table_rows,
            schema=pa.schema(fields),
        )
    return result


def _all_tables(
    source_tables: Mapping[str, pa.Table],
    crosswalk: PublicationCrosswalk,
) -> dict[str, pa.Table]:
    typed = {
        **_entity_tables(source_tables, crosswalk),
        **_relationship_tables(source_tables, crosswalk),
    }
    carried = {
        f"l4_{name}": table
        for name, table in source_tables.items()
    }
    if set(typed).intersection(carried):
        raise L5aPublicationError(
            "L5A_PHYSICAL_TABLE_COLLISION",
            "crosswalk physical tables collide with carried L4 tables",
        )
    return dict(sorted({**typed, **carried}.items()))


def _required_member_snapshots(
    source: SealedL4ServingSource,
    tables: Mapping[str, pa.Table],
) -> tuple[L5aRequiredMemberSnapshot, ...]:
    manifest_table = tables["l4_semantic_required_member_manifests"]
    member_table = tables["l4_semantic_required_members"]
    return _required_member_snapshots_from_rows(
        source,
        manifest_table.to_pylist(),
        member_table.to_pylist(),
    )


def _required_member_snapshots_from_rows(
    source: SealedL4ServingSource,
    manifest_rows: Sequence[Mapping[str, Any]],
    member_rows: Sequence[Mapping[str, Any]],
) -> tuple[L5aRequiredMemberSnapshot, ...]:
    members_by_manifest: dict[str, list[dict[str, Any]]] = {}
    for source_row in member_rows:
        row = dict(source_row)
        members_by_manifest.setdefault(
            str(row["required_member_manifest_id"]),
            [],
        ).append(row)
    schema_hash = canonical_sha256(RequiredMemberManifestV1_1.model_json_schema())
    snapshots = []
    for source_manifest in sorted(
        manifest_rows,
        key=lambda row: str(row["required_member_manifest_id"]),
    ):
        manifest = dict(source_manifest)
        manifest_id = str(manifest["required_member_manifest_id"])
        members = sorted(
            members_by_manifest.pop(manifest_id, []),
            key=lambda row: int(row["manifest_member_index"]),
        )
        if int(manifest["member_count"]) != len(members):
            raise L5aPublicationError(
                "L5A_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"member count differs for {manifest_id}",
            )
        canonical_ids = (
            f"required-member-manifest|{manifest_id}",
            *(
                "required-member|"
                f"{manifest_id}|{int(member['manifest_member_index'])}|"
                f"{member['member_canonical_id']}"
                for member in members
            ),
        )
        snapshots.append(L5aRequiredMemberSnapshot(
            required_member_manifest_id=manifest_id,
            required_member_manifest_schema_hash=schema_hash,
            required_member_manifest_hash=str(manifest["manifest_hash"]),
            authoritative_collection_hash=str(
                manifest["authoritative_collection_hash"]
            ),
            source_artifact_manifest_id=(
                source.input_manifest.artifact_manifest_id
            ),
            source_artifact_manifest_hash=source.input_manifest.manifest_hash,
            canonical_ids=tuple(canonical_ids),
            row_fingerprint=canonical_sha256({
                "manifest": manifest,
                "members": members,
            }),
        ))
    if members_by_manifest:
        raise L5aPublicationError(
            "L5A_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
            "required-member rows reference an unknown manifest",
        )
    return tuple(snapshots)


def _ancestor_paths(crosswalk: PublicationCrosswalk) -> dict[str, tuple[str, ...]]:
    parents = {
        item.canonical_semantic_type_id: item.canonical_parent_semantic_type_id
        for item in crosswalk.semantic_type_mappings
    }
    paths: dict[str, tuple[str, ...]] = {}
    for type_id in sorted(parents):
        path: list[str] = []
        current = parents[type_id]
        while current is not None:
            if current in path or current == type_id:
                raise L5aPublicationError(
                    "L5A_HIERARCHY_CYCLE",
                    f"crosswalk hierarchy contains a cycle at {type_id}",
                )
            path.append(current)
            current = parents[current]
        paths[type_id] = tuple(path)
    return paths


def _definitions(
    source: SealedL4ServingSource,
    crosswalks: Sequence[PublicationCrosswalk],
    table_snapshots: Sequence[L5aTableSnapshot],
    required_member_snapshots: Sequence[L5aRequiredMemberSnapshot],
    target_ids: Mapping[L5ATargetKind, str],
    access_policy: AccessPolicy,
) -> dict[L5ATargetKind, dict[str, Any]]:
    crosswalk = _canonical_crosswalk(crosswalks)
    authorities = [
        item.authority.model_dump(mode="json")
        for item in sorted(
            crosswalks,
            key=lambda item: item.authority.required_member_manifest_id,
        )
    ]
    common = {
        "publication_version": L5A_TARGET_VERSION,
        "publication_code_version": L5A_PUBLICATION_CODE_VERSION,
        "source_projection_id": source.projection.projection_id,
        "source_projection_hash": source.projection.projection_hash,
        "source_artifact_manifest_id": source.input_manifest.artifact_manifest_id,
        "source_artifact_manifest_hash": source.input_manifest.manifest_hash,
        "publication_crosswalk_ids": [
            item.publication_crosswalk_id for item in crosswalks
        ],
        "publication_crosswalk_hashes": [
            item.crosswalk_hash for item in crosswalks
        ],
        "authorities": authorities,
        "access_policy_id": access_policy.access_policy_id,
        "access_policy_hash": access_policy.policy_hash,
        "tables": [item.as_dict() for item in table_snapshots],
        "required_member_snapshots": [
            item.as_dict() for item in required_member_snapshots
        ],
    }
    type_by_id = {
        item.canonical_semantic_type_id: item
        for item in crosswalk.semantic_type_mappings
    }
    ancestors = _ancestor_paths(crosswalk)
    semantic_types = [
        {
            "canonical_semantic_type_id": item.canonical_semantic_type_id,
            "canonical_parent_semantic_type_id": (
                item.canonical_parent_semantic_type_id
            ),
            "physical_table_id": item.physical_table_id,
            "instance_key_property_ids": list(
                item.canonical_instance_key_property_ids
            ),
            "physical_identity_source": "stable_canonical_entity_id",
            "physical_identity_column": "__canonical_id",
            "properties": [
                {
                    "canonical_property_id": prop.canonical_property_id,
                    "physical_column_id": prop.physical_column_id,
                    "materialization": "schema_only",
                }
                for prop in item.property_mappings
            ],
        }
        for item in crosswalk.semantic_type_mappings
    ]
    relationships = [
        {
            "canonical_semantic_relationship_id": (
                item.canonical_semantic_relationship_id
            ),
            "source_semantic_type_id": item.source_semantic_type_id,
            "target_semantic_type_id": item.target_semantic_type_id,
            "physical_table_id": item.physical_table_id,
            "source_identity_column": "__source_entity_id",
            "target_identity_column": "__target_entity_id",
            "source_key_fields": [
                {
                    **field.model_dump(mode="json"),
                    "materialization": "schema_only",
                }
                for field in item.source_key_fields
            ],
            "target_key_fields": [
                {
                    **field.model_dump(mode="json"),
                    "materialization": "schema_only",
                }
                for field in item.target_key_fields
            ],
        }
        for item in crosswalk.relationship_mappings
    ]
    return {
        "parquet": {
            **common,
            "target_kind": "parquet",
            "target_id": target_ids["parquet"],
            "format": "parquet",
            "semantic_types": semantic_types,
            "relationships": relationships,
        },
        "semantic_model": {
            **common,
            "target_kind": "semantic_model",
            "target_id": target_ids["semantic_model"],
            "storage_mode": "direct_lake",
            "source_surfaces": semantic_types,
            "relationships": relationships,
        },
        "ontology": {
            **common,
            "target_kind": "ontology",
            "target_id": target_ids["ontology"],
            "native_inheritance_assumed": False,
            "entity_types": [
                {
                    "id": str(item.ontology_bigint_id),
                    "canonical_semantic_type_id": (
                        item.canonical_semantic_type_id
                    ),
                    "base_entity_type_id": None,
                    "flattened_ancestor_type_ids": list(
                        ancestors[item.canonical_semantic_type_id]
                    ),
                    "physical_table_id": item.physical_table_id,
                    "physical_identity_source": "stable_canonical_entity_id",
                    "physical_identity_column": "__canonical_id",
                    "properties": [
                        {
                            "id": str(prop.ontology_bigint_id),
                            "canonical_property_id": prop.canonical_property_id,
                            "physical_column_id": prop.physical_column_id,
                            "materialization": "schema_only",
                        }
                        for prop in item.property_mappings
                    ],
                }
                for item in crosswalk.semantic_type_mappings
            ],
            "relationship_types": [
                {
                    "id": str(item.ontology_bigint_id),
                    "canonical_semantic_relationship_id": (
                        item.canonical_semantic_relationship_id
                    ),
                    "source_entity_type_id": str(
                        type_by_id[
                            item.source_semantic_type_id
                        ].ontology_bigint_id
                    ),
                    "target_entity_type_id": str(
                        type_by_id[
                            item.target_semantic_type_id
                        ].ontology_bigint_id
                    ),
                    "physical_table_id": item.physical_table_id,
                    "source_identity_column": "__source_entity_id",
                    "target_identity_column": "__target_entity_id",
                }
                for item in crosswalk.relationship_mappings
            ],
        },
        "graph": {
            **common,
            "target_kind": "graph",
            "target_id": target_ids["graph"],
            "node_types": [
                {
                    "canonical_semantic_type_id": (
                        item.canonical_semantic_type_id
                    ),
                    "label": item.graph_label,
                    "aliases": list(item.graph_aliases),
                    "physical_table_id": item.physical_table_id,
                    "physical_identity_source": "stable_canonical_entity_id",
                    "physical_identity_column": "__canonical_id",
                    "key_properties": ["__canonical_id"],
                    "properties": [
                        {
                            "canonical_property_id": prop.canonical_property_id,
                            "graph_property": prop.graph_property,
                            "physical_column_id": prop.physical_column_id,
                            "materialization": "schema_only",
                        }
                        for prop in item.property_mappings
                    ],
                }
                for item in crosswalk.semantic_type_mappings
            ],
            "edge_types": [
                {
                    "canonical_semantic_relationship_id": (
                        item.canonical_semantic_relationship_id
                    ),
                    "label": item.graph_label,
                    "aliases": list(item.graph_aliases),
                    "source_label": type_by_id[
                        item.source_semantic_type_id
                    ].graph_label,
                    "target_label": type_by_id[
                        item.target_semantic_type_id
                    ].graph_label,
                    "physical_table_id": item.physical_table_id,
                    "source_identity_column": "__source_entity_id",
                    "target_identity_column": "__target_entity_id",
                    "source_key_fields": [
                        {
                            **field.model_dump(mode="json"),
                            "materialization": "schema_only",
                        }
                        for field in item.source_key_fields
                    ],
                    "target_key_fields": [
                        {
                            **field.model_dump(mode="json"),
                            "materialization": "schema_only",
                        }
                        for field in item.target_key_fields
                    ],
                }
                for item in crosswalk.relationship_mappings
            ],
        },
    }


def _governed_asset_identity(
    source: SealedL4ServingSource,
    *,
    kind: L5ATargetKind,
    target_id: str,
    content_hash: str,
    locator: ImmutableSourceLocator,
    asset_version_id: str,
) -> CanonicalIdentityEnvelope:
    values = _identity(
        source,
        contract_kind="c0.governed_asset_reference",
    ).model_dump(mode="python")
    values.update({
        "source_file_id": f"l5a-definition:{kind}",
        "asset_id": target_id,
        "asset_version_id": asset_version_id,
        "content_hash": content_hash,
        "immutable_locator": locator,
    })
    return CanonicalIdentityEnvelope.model_validate(values)


def _validate_governed_assets(
    source: SealedL4ServingSource,
    definitions: Mapping[L5ATargetKind, Mapping[str, Any]],
    target_ids: Mapping[L5ATargetKind, str],
    access_policy: AccessPolicy,
    governed_assets: Sequence[GovernedAssetReference],
) -> None:
    assets_by_target = {asset.asset_id: asset for asset in governed_assets}
    if (
        len(assets_by_target) != len(governed_assets)
        or set(assets_by_target) != set(target_ids.values())
    ):
        raise L5aPublicationError(
            "L5A_GOVERNED_ASSET_AUTHORITY_MISMATCH",
            "governed assets must exactly cover the four target definitions",
        )
    for kind in L5A_TARGET_ORDER:
        target_id = target_ids[kind]
        asset = assets_by_target[target_id]
        expected_content_hash = canonical_sha256(definitions[kind])
        if (
            asset.source_file_id != f"l5a-definition:{kind}"
            or asset.asset_id != target_id
            or asset.content_hash != expected_content_hash
            or asset.storage_reference.object_id != target_id
            or asset.asset_version_id
            != asset.storage_reference.object_version_id
            or asset.asset_kind != "derived"
            or asset.identity.project_id != source.receipt.identity.project_id
            or asset.identity.domain_schema_version != "2.0"
            or asset.identity.domain_contract_hash
            != source.receipt.identity.domain_contract_hash
            or asset.identity.semantic_contract_hash
            != source.projection.sealed_semantic_contract_hash
            or source.input_manifest.artifact_manifest_id
            not in asset.identity.parent_artifact_ids
        ):
            raise L5aPublicationError(
                "L5A_GOVERNED_ASSET_AUTHORITY_MISMATCH",
                f"governed asset for {kind} differs from the persisted definition",
            )
        try:
            asset.validate_access_policy(access_policy)
        except ValueError as exc:
            raise L5aPublicationError(
                "L5A_ACCESS_POLICY_MISMATCH",
                str(exc),
            ) from exc


def build_l5a_governed_assets(
    source: SealedL4ServingSource,
    *,
    crosswalks: Sequence[PublicationCrosswalk],
    access_policy: AccessPolicy,
    target_ids: Mapping[L5ATargetKind, str],
    storage_references: Mapping[L5ATargetKind, StorageReference],
    immutable_locators: Mapping[L5ATargetKind, ImmutableSourceLocator],
) -> tuple[GovernedAssetReference, ...]:
    """Build output asset references from exact compiled target definitions."""

    if (
        set(target_ids) != set(L5A_TARGET_ORDER)
        or set(storage_references) != set(L5A_TARGET_ORDER)
        or set(immutable_locators) != set(L5A_TARGET_ORDER)
    ):
        raise L5aPublicationError(
            "L5A_GOVERNED_ASSET_INPUT_MISMATCH",
            "target, storage, and locator maps must cover all L5a targets",
        )
    ordered_crosswalks = tuple(sorted(
        crosswalks,
        key=lambda item: item.authority.required_member_manifest_id,
    ))
    source_tables = _load_source_tables(source)
    _validate_publish_authority(
        source,
        source_tables,
        ordered_crosswalks,
        access_policy,
    )
    tables = _all_tables(source_tables, _canonical_crosswalk(ordered_crosswalks))
    snapshots = tuple(
        _table_snapshot(table_id, table)
        for table_id, table in sorted(tables.items())
    )
    required_member_snapshots = _required_member_snapshots(source, tables)
    definitions = _definitions(
        source,
        ordered_crosswalks,
        snapshots,
        required_member_snapshots,
        target_ids,
        access_policy,
    )
    result = []
    for kind in L5A_TARGET_ORDER:
        storage = storage_references[kind]
        locator = immutable_locators[kind]
        target_id = target_ids[kind]
        if storage.object_id != target_id:
            raise L5aPublicationError(
                "L5A_GOVERNED_ASSET_INPUT_MISMATCH",
                f"{kind} storage object must equal its target ID",
            )
        content_hash = canonical_sha256(definitions[kind])
        identity = _governed_asset_identity(
            source,
            kind=kind,
            target_id=target_id,
            content_hash=content_hash,
            locator=locator,
            asset_version_id=storage.object_version_id,
        )
        values = {
            "identity": identity,
            "governed_asset_reference_id": deterministic_contract_id(
                "governed-asset-reference",
                {
                    "target_kind": kind,
                    "target_id": target_id,
                    "content_hash": content_hash,
                    "storage_reference_hash": storage.storage_reference_hash,
                },
            ),
            "asset_kind": "derived",
            "source_file_id": f"l5a-definition:{kind}",
            "asset_id": target_id,
            "asset_version_id": storage.object_version_id,
            "immutable_locator": locator,
            "content_hash": content_hash,
            "storage_reference": storage,
            "access_policy_id": access_policy.access_policy_id,
            "access_policy_hash": access_policy.policy_hash,
            "on_demand_url_policy": "not_permitted",
        }
        result.append(GovernedAssetReference(
            **values,
            asset_reference_hash=canonical_sha256(values),
        ))
    return tuple(result)


def l5a_input_fingerprint(
    source: SealedL4ServingSource,
    *,
    crosswalks: Sequence[PublicationCrosswalk],
    access_policy: AccessPolicy,
    governed_assets: Sequence[GovernedAssetReference],
    target_ids: Mapping[L5ATargetKind, str],
) -> str:
    return canonical_sha256({
        "stage": L5A_STAGE_NAME,
        "stage_contract_version": L5A_STAGE_CONTRACT_VERSION,
        "publication_code_version": L5A_PUBLICATION_CODE_VERSION,
        "l4_receipt_hash": source.receipt.receipt_hash,
        "l4_output_manifest_hash": source.manifest.manifest_hash,
        "l3_artifact_manifest_id": source.input_manifest.artifact_manifest_id,
        "l3_artifact_manifest_hash": source.input_manifest.manifest_hash,
        "source_projection_hash": source.projection.projection_hash,
        "crosswalk_hashes": sorted(item.crosswalk_hash for item in crosswalks),
        "access_policy_hash": access_policy.policy_hash,
        "governed_asset_hashes": sorted(
            item.asset_reference_hash for item in governed_assets
        ),
        "target_ids": dict(sorted(target_ids.items())),
    })


def compile_l5a_publication(
    source: SealedL4ServingSource,
    *,
    crosswalks: Sequence[PublicationCrosswalk],
    access_policy: AccessPolicy,
    governed_assets: Sequence[GovernedAssetReference],
    target_ids: Mapping[L5ATargetKind, str],
) -> L5aCompiledPublication:
    """Compile deterministic L5a physical tables and structured definitions."""

    if not isinstance(source, SealedL4ServingSource):
        raise L5aPublicationError(
            "L5A_SCHEMA2_SOURCE_REQUIRED",
            "L5a accepts only SealedL4ServingSource",
        )
    if set(target_ids) != set(L5A_TARGET_ORDER):
        raise L5aPublicationError(
            "L5A_TARGET_SET_MISMATCH",
            "targets must be exactly parquet, semantic_model, ontology, and graph",
        )
    if any(not value.strip() for value in target_ids.values()):
        raise L5aPublicationError(
            "L5A_TARGET_ID_INVALID",
            "target IDs must be non-empty",
        )
    ordered_crosswalks = tuple(sorted(
        crosswalks,
        key=lambda item: item.authority.required_member_manifest_id,
    ))
    ordered_assets = tuple(sorted(
        governed_assets,
        key=lambda item: item.governed_asset_reference_id,
    ))
    source_tables = _load_source_tables(source)
    _validate_publish_authority(
        source,
        source_tables,
        ordered_crosswalks,
        access_policy,
    )
    crosswalk = _canonical_crosswalk(ordered_crosswalks)
    tables = _all_tables(source_tables, crosswalk)
    snapshots = tuple(
        _table_snapshot(table_id, table)
        for table_id, table in sorted(tables.items())
    )
    required_member_snapshots = _required_member_snapshots(source, tables)
    required_member_manifest_rows = tuple(
        dict(row)
        for row in tables[
            "l4_semantic_required_member_manifests"
        ].to_pylist()
    )
    required_member_rows = tuple(
        dict(row)
        for row in tables["l4_semantic_required_members"].to_pylist()
    )
    typed_target_ids = {
        kind: str(target_ids[kind]) for kind in L5A_TARGET_ORDER
    }
    definitions = _definitions(
        source,
        ordered_crosswalks,
        snapshots,
        required_member_snapshots,
        typed_target_ids,
        access_policy,
    )
    _validate_governed_assets(
        source,
        definitions,
        typed_target_ids,
        access_policy,
        ordered_assets,
    )
    return L5aCompiledPublication(
        source=source,
        fingerprint=l5a_input_fingerprint(
            source,
            crosswalks=ordered_crosswalks,
            access_policy=access_policy,
            governed_assets=ordered_assets,
            target_ids=typed_target_ids,
        ),
        crosswalks=ordered_crosswalks,
        access_policy=access_policy,
        governed_assets=ordered_assets,
        target_ids=typed_target_ids,
        definitions=definitions,
        tables=tables,
        table_snapshots=snapshots,
        required_member_manifest_rows=required_member_manifest_rows,
        required_member_rows=required_member_rows,
        required_member_snapshots=required_member_snapshots,
    )


def _safe_table_file(table_id: str, ordinal: int) -> Path:
    digest = hashlib.sha256(table_id.encode("utf-8")).hexdigest()[:16]
    return Path("tables") / f"{ordinal:03d}-{digest}.parquet"


def _persist_compiled(
    compiled: L5aCompiledPublication,
    root: Path,
) -> tuple[dict[L5ATargetKind, Path], dict[str, Path]]:
    definition_paths: dict[L5ATargetKind, Path] = {}
    for kind in L5A_TARGET_ORDER:
        path = root / "definitions" / f"{kind}.json"
        _write_json(path, compiled.definitions[kind])
        definition_paths[kind] = path
    table_paths: dict[str, Path] = {}
    for ordinal, (table_id, table) in enumerate(sorted(compiled.tables.items())):
        relative = _safe_table_file(table_id, ordinal)
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            table,
            path,
            compression="zstd",
            version="2.6",
            use_dictionary=False,
            write_statistics=True,
        )
        read_back = pq.read_table(path)
        if _table_snapshot(table_id, read_back) != _table_snapshot(table_id, table):
            raise L5aPublicationError(
                "L5A_LOCAL_MATERIALIZATION_DRIFT",
                f"persisted Parquet differs for {table_id}",
            )
        table_paths[table_id] = path
    _write_json(root / "publication-crosswalks.json", compiled.crosswalks)
    _write_json(root / "access-policy.json", compiled.access_policy)
    _write_json(root / "governed-assets.json", compiled.governed_assets)
    return definition_paths, table_paths


def _write_json(path: Path, value: Any) -> bytes:
    payload = (canonical_json(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _expected_state(
    compiled: L5aCompiledPublication,
    kind: L5ATargetKind,
    *,
    publication_token: str,
) -> L5aTargetState:
    return L5aTargetState(
        target_kind=kind,
        target_id=compiled.target_ids[kind],
        target_version=L5A_TARGET_VERSION,
        definition=compiled.definitions[kind],
        table_snapshots=compiled.table_snapshots,
        access_policy_id=compiled.access_policy.access_policy_id,
        access_policy_hash=compiled.access_policy.policy_hash,
        publication_token=publication_token,
        required_member_manifest_rows=compiled.required_member_manifest_rows,
        required_member_rows=compiled.required_member_rows,
    )


def _validate_state(
    actual: L5aTargetState | None,
    expected: L5aTargetState,
    *,
    phase: str,
) -> None:
    if actual is None:
        raise L5aPublicationError(
            "L5A_TARGET_MISSING",
            f"{expected.target_kind} target is missing during {phase}",
        )
    if actual.target_kind != expected.target_kind or actual.target_id != expected.target_id:
        raise L5aPublicationError(
            "L5A_TARGET_IDENTITY_DRIFT",
            f"{expected.target_kind} target identity differs during {phase}",
        )
    if actual.target_version != L5A_TARGET_VERSION:
        raise L5aPublicationError(
            "L5A_TARGET_VERSION_UNSUPPORTED",
            f"{expected.target_kind} has unsupported version "
            f"{actual.target_version!r}",
        )
    if canonical_sha256(actual.definition) != canonical_sha256(expected.definition):
        raise L5aPublicationError(
            "L5A_TARGET_DEFINITION_DRIFT",
            f"{expected.target_kind} definition differs during {phase}",
        )
    if actual.table_snapshots != expected.table_snapshots:
        raise L5aPublicationError(
            "L5A_TARGET_TABLE_DRIFT",
            f"{expected.target_kind} table read-back differs during {phase}",
        )
    if (
        actual.required_member_manifest_rows
        != expected.required_member_manifest_rows
        or actual.required_member_rows != expected.required_member_rows
    ):
        raise L5aPublicationError(
            "L5A_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
            f"{expected.target_kind} required-member read-back differs during {phase}",
        )
    if (
        actual.access_policy_id != expected.access_policy_id
        or actual.access_policy_hash != expected.access_policy_hash
    ):
        raise L5aPublicationError(
            "L5A_ACCESS_POLICY_MISMATCH",
            f"{expected.target_kind} access policy differs during {phase}",
        )
    if actual.publication_token != expected.publication_token:
        raise L5aPublicationError(
            "L5A_TARGET_OWNERSHIP_DRIFT",
            f"{expected.target_kind} publication token differs during {phase}",
        )


def _projection_evidence(
    kind: L5ATargetKind,
    state: L5aTargetState,
    snapshot: L5aRequiredMemberSnapshot,
) -> ProjectionEvidence:
    values: dict[str, Any] = {
        "count": len(snapshot.canonical_ids),
        "canonical_id_set_hash": canonical_sha256(snapshot.canonical_ids),
        "row_fingerprint": None,
        "definition_fingerprint": None,
        "index_fingerprint": None,
    }
    if kind == "parquet":
        values["row_fingerprint"] = snapshot.row_fingerprint
    else:
        values["definition_fingerprint"] = canonical_sha256({
            "definition": state.definition,
            "required_member_snapshot": snapshot.as_dict(),
        })
    return ProjectionEvidence(**values)


def _equivalences(
    compiled: L5aCompiledPublication,
    states: Mapping[L5ATargetKind, L5aTargetState],
) -> tuple[ProjectionEquivalence, ...]:
    result = []
    compiled_by_manifest = {
        item.required_member_manifest_id: item
        for item in compiled.required_member_snapshots
    }
    for crosswalk in compiled.crosswalks:
        manifest_id = crosswalk.authority.required_member_manifest_id
        expected_snapshot = compiled_by_manifest.get(manifest_id)
        if expected_snapshot is None:
            raise L5aPublicationError(
                "L5A_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"compiled required-member snapshot missing for {manifest_id}",
            )
        authority = crosswalk.authority
        if (
            expected_snapshot.required_member_manifest_schema_hash
            != authority.required_member_manifest_schema_hash
            or expected_snapshot.required_member_manifest_hash
            != authority.required_member_manifest_hash
            or expected_snapshot.authoritative_collection_hash
            != authority.authoritative_collection_hash
            or expected_snapshot.source_artifact_manifest_id
            != authority.source_artifact_manifest_id
            or expected_snapshot.source_artifact_manifest_hash
            != authority.source_artifact_manifest_hash
        ):
            raise L5aPublicationError(
                "L5A_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"compiled authority differs for {manifest_id}",
            )
        for kind in L5A_TARGET_ORDER:
            state = states[kind]
            read_back_snapshots = _required_member_snapshots_from_rows(
                compiled.source,
                state.required_member_manifest_rows,
                state.required_member_rows,
            )
            state_by_manifest = {
                item.required_member_manifest_id: item
                for item in read_back_snapshots
            }
            read_back_snapshot = state_by_manifest.get(manifest_id)
            if read_back_snapshot is None:
                raise L5aPublicationError(
                    "L5A_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                    f"{kind} read-back omits {manifest_id}",
                )
            missing = tuple(sorted(
                set(expected_snapshot.canonical_ids)
                - set(read_back_snapshot.canonical_ids)
            ))
            extra = tuple(sorted(
                set(read_back_snapshot.canonical_ids)
                - set(expected_snapshot.canonical_ids)
            ))
            expected_evidence = _projection_evidence(
                kind,
                _expected_state(
                    compiled,
                    kind,
                    publication_token=state.publication_token,
                ),
                expected_snapshot,
            )
            read_back_evidence = _projection_evidence(
                kind,
                state,
                read_back_snapshot,
            )
            if (
                missing
                or extra
                or expected_snapshot != read_back_snapshot
                or expected_evidence != read_back_evidence
            ):
                raise L5aPublicationError(
                    "L5A_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                    f"{kind} required-member rows differ for {manifest_id}; "
                    f"missing={missing}, extra={extra}",
                )
            values = {
                "identity": _identity(
                    compiled.source,
                    contract_kind="c0.projection_equivalence",
                ),
                "projection_equivalence_id": deterministic_contract_id(
                    "projection-equivalence",
                    {
                        "stage": "L5a",
                        "target_kind": kind,
                        "target_id": state.target_id,
                        "required_member_manifest_id": (
                            crosswalk.authority.required_member_manifest_id
                        ),
                        "fingerprint": compiled.fingerprint,
                    },
                ),
                "authority": crosswalk.authority,
                "publication_crosswalk_id": crosswalk.publication_crosswalk_id,
                "publication_crosswalk_hash": crosswalk.crosswalk_hash,
                "source_projection_id": compiled.source.projection.projection_id,
                "source_projection_hash": compiled.source.projection.projection_hash,
                "projection_kind": kind,
                "expected": expected_evidence,
                "compiled": expected_evidence,
                "deployed": read_back_evidence,
                "read_back": read_back_evidence,
                "missing_canonical_ids": (),
                "extra_canonical_ids": (),
                "equivalent": True,
            }
            result.append(ProjectionEquivalence(
                **values,
                equivalence_hash=canonical_sha256(values),
            ))
    return tuple(result)


def _artifact_entry(
    *,
    artifact_id: str,
    contract_kind: str,
    schema_hash: str,
    path: Path,
    row_count: int | None,
    canonical_id_set_hash: str | None,
    partition_count: int = 1,
) -> ArtifactEntry:
    payload = path.read_bytes()
    return ArtifactEntry(
        artifact_id=artifact_id,
        contract_kind=contract_kind,
        contract_version="1.0.0",
        schema_hash=schema_hash,
        content_hash=hashlib.sha256(payload).hexdigest(),
        canonical_id_set_hash=canonical_id_set_hash,
        row_count=row_count,
        byte_count=len(payload),
        partition_count=partition_count,
        media_type=(
            "application/vnd.apache.parquet"
            if path.suffix == ".parquet"
            else "application/json"
        ),
        immutable_locator=None,
        blob_asset_ref_id=None,
    )


def _output_manifest(
    compiled: L5aCompiledPublication,
    root: Path,
    equivalences: Sequence[ProjectionEquivalence],
    definition_paths: Mapping[L5ATargetKind, Path],
    table_paths: Mapping[str, Path],
) -> ArtifactManifest:
    entries: list[ArtifactEntry] = []
    for kind, path in definition_paths.items():
        entries.append(_artifact_entry(
            artifact_id=f"l5a-definition:{kind}",
            contract_kind=f"l5a.{kind}_definition",
            schema_hash=canonical_sha256({
                "target_kind": kind,
                "publication_version": L5A_TARGET_VERSION,
            }),
            path=path,
            row_count=1,
            canonical_id_set_hash=None,
        ))
    snapshot_by_id = {
        item.table_id: item for item in compiled.table_snapshots
    }
    for table_id, path in table_paths.items():
        snapshot = snapshot_by_id[table_id]
        entries.append(_artifact_entry(
            artifact_id=f"l5a-table:{table_id}",
            contract_kind="l5a.parquet_table",
            schema_hash=snapshot.schema_hash,
            path=path,
            row_count=snapshot.row_count,
            canonical_id_set_hash=snapshot.canonical_id_set_hash,
        ))
    json_artifacts = (
        (
            "l5a-publication-crosswalks",
            "c0.publication_crosswalk",
            root / "publication-crosswalks.json",
            canonical_sha256({
                "type": "array",
                "items": PublicationCrosswalk.model_json_schema(),
            }),
            len(compiled.crosswalks),
        ),
        (
            "l5a-access-policy",
            "c0.access_policy",
            root / "access-policy.json",
            canonical_sha256(AccessPolicy.model_json_schema()),
            1,
        ),
        (
            "l5a-governed-assets",
            "c0.governed_asset_reference",
            root / "governed-assets.json",
            canonical_sha256({
                "type": "array",
                "items": GovernedAssetReference.model_json_schema(),
            }),
            len(compiled.governed_assets),
        ),
        (
            "l5a-projection-equivalence",
            "c0.projection_equivalence",
            root / "projection-equivalence.json",
            canonical_sha256({
                "type": "array",
                "items": ProjectionEquivalence.model_json_schema(),
            }),
            len(equivalences),
        ),
    )
    for artifact_id, kind, path, schema_hash, row_count in json_artifacts:
        entries.append(_artifact_entry(
            artifact_id=artifact_id,
            contract_kind=kind,
            schema_hash=schema_hash,
            path=path,
            row_count=row_count,
            canonical_id_set_hash=None,
        ))
    ordered = tuple(sorted(entries, key=lambda item: item.artifact_id))
    values = {
        "identity": _identity(
            compiled.source,
            contract_kind="c0.artifact_manifest",
        ),
        "artifact_manifest_id": deterministic_contract_id(
            "artifact-manifest",
            {"stage": "L5a", "fingerprint": compiled.fingerprint},
        ),
        "entries": ordered,
        "total_row_count": sum(entry.row_count or 0 for entry in ordered),
        "total_byte_count": sum(entry.byte_count for entry in ordered),
    }
    return ArtifactManifest(
        **values,
        manifest_hash=canonical_sha256(values),
    )


def _metrics(
    compiled: L5aCompiledPublication,
    *,
    started: float,
    accounting: _CallAccounting,
    storage_write_bytes: int,
    fabric_rows_read: int,
    fabric_rows_written: int,
    cache_hits: int,
) -> StageResourceMetrics:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak_rss = int(usage.ru_maxrss)
    if os.uname().sysname != "Darwin":
        peak_rss *= 1024
    values = {
        "identity": _identity(
            compiled.source,
            contract_kind="c0.stage_resource_metrics",
        ),
        "resource_metrics_id": deterministic_contract_id(
            "stage-resource-metrics",
            {
                "stage": "L5a",
                "fingerprint": compiled.fingerprint,
                "fabric_calls": accounting.fabric_calls,
                "cache_hits": cache_hits,
            },
        ),
        "stage_id": "L5",
        "stage_name": L5A_STAGE_NAME,
        "wall_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "cpu_ms": max(0, int(time.process_time() * 1000)),
        "peak_rss_bytes": peak_rss,
        "storage_read_bytes": compiled.source.manifest.total_byte_count,
        "storage_write_bytes": storage_write_bytes,
        "network_request_bytes": accounting.network_request_bytes,
        "network_response_bytes": accounting.network_response_bytes,
        "source_units_read": 0,
        "source_units_written": 0,
        "source_units_skipped": 0,
        "document_intelligence_calls": 0,
        "document_intelligence_pages": 0,
        "foundry_calls": 0,
        "foundry_input_tokens": 0,
        "foundry_output_tokens": 0,
        "embedding_calls": 0,
        "embedding_items": 0,
        "fabric_calls": accounting.fabric_calls,
        "fabric_rows_read": fabric_rows_read,
        "fabric_rows_written": fabric_rows_written,
        "search_calls": 0,
        "search_documents_read": 0,
        "search_documents_written": 0,
        "retry_count": accounting.retry_count,
        "retry_wait_ms": accounting.retry_wait_ms,
        "cache_hits": cache_hits,
        "cache_misses": 0 if cache_hits else 1,
        "max_observed_concurrency": 1,
        "budget_snapshot_hash": _budget_snapshot_hash(),
        "exceeded_dimensions": accounting.exceeded_dimensions,
    }
    return StageResourceMetrics(
        **values,
        metrics_hash=canonical_sha256(values),
    )


def _receipt(
    compiled: L5aCompiledPublication,
    *,
    status: Literal["succeeded", "skipped", "failed"],
    output_manifest: ArtifactManifest | None,
    metrics: StageResourceMetrics,
    accounting: _CallAccounting,
    started_at_utc: datetime,
    error_codes: Sequence[str] = (),
) -> StageReceipt:
    values = {
        "identity": _identity(compiled.source, contract_kind="c0.stage_receipt"),
        "stage_receipt_id": deterministic_contract_id(
            "stage-receipt",
            {
                "stage": "L5a",
                "fingerprint": compiled.fingerprint,
                "status": status,
                "fabric_calls": accounting.fabric_calls,
            },
        ),
        "stage_id": "L5",
        "stage_name": L5A_STAGE_NAME,
        "stage_contract_version": L5A_STAGE_CONTRACT_VERSION,
        "status": status,
        "input_manifest_id": compiled.source.manifest.artifact_manifest_id,
        "input_manifest_hash": compiled.source.manifest.manifest_hash,
        "output_manifest_id": (
            output_manifest.artifact_manifest_id if output_manifest else None
        ),
        "output_manifest_hash": (
            output_manifest.manifest_hash if output_manifest else None
        ),
        "skip_key": compiled.fingerprint,
        "accepted_contract_versions": L5A_ACCEPTED_VERSIONS,
        "resource_metrics_id": metrics.resource_metrics_id,
        "resource_metrics_hash": metrics.metrics_hash,
        "attempt_count": 1,
        "remote_operation_refs": accounting.remote_operation_refs,
        "error_codes": tuple(sorted(set(error_codes))),
        "started_at_utc": started_at_utc,
        "completed_at_utc": datetime.now(timezone.utc),
    }
    return StageReceipt(
        **values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )


def _persisted_files(root: Path) -> set[Path]:
    return {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file()
    }


def _artifact_path(entry: ArtifactEntry, root: Path) -> Path:
    if entry.artifact_id.startswith("l5a-definition:"):
        return root / "definitions" / (
            entry.artifact_id.removeprefix("l5a-definition:") + ".json"
        )
    if entry.artifact_id.startswith("l5a-table:"):
        table_id = entry.artifact_id.removeprefix("l5a-table:")
        digest = hashlib.sha256(table_id.encode("utf-8")).hexdigest()[:16]
        candidates = list((root / "tables").glob(f"*-{digest}.parquet"))
        if len(candidates) != 1:
            raise L5aPublicationError(
                "L5A_OUTPUT_MANIFEST_INVALID",
                f"could not resolve table artifact {entry.artifact_id}",
            )
        return candidates[0]
    mapping = {
        "l5a-publication-crosswalks": "publication-crosswalks.json",
        "l5a-access-policy": "access-policy.json",
        "l5a-governed-assets": "governed-assets.json",
        "l5a-projection-equivalence": "projection-equivalence.json",
    }
    try:
        return root / mapping[entry.artifact_id]
    except KeyError as exc:
        raise L5aPublicationError(
            "L5A_OUTPUT_MANIFEST_INVALID",
            f"unknown artifact {entry.artifact_id}",
        ) from exc


def _existing_is_intact(
    compiled: L5aCompiledPublication,
    run_root: Path,
) -> tuple[
    ArtifactManifest,
    StageResourceMetrics,
    StageReceipt,
    tuple[ProjectionEquivalence, ...],
] | None:
    if not run_root.is_dir():
        return None
    try:
        manifest = ArtifactManifest.model_validate_json(
            (run_root / "output-manifest.json").read_text("utf-8")
        )
        metrics = StageResourceMetrics.model_validate_json(
            (run_root / "resource-metrics.json").read_text("utf-8")
        )
        receipt = StageReceipt.model_validate_json(
            (run_root / "stage-receipt.json").read_text("utf-8")
        )
        equivalences = tuple(
            ProjectionEquivalence.model_validate(item)
            for item in json.loads(
                (run_root / "projection-equivalence.json").read_text("utf-8")
            )
        )
    except (
        OSError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ):
        return None
    if (
        receipt.status != "succeeded"
        or receipt.stage_id != "L5"
        or receipt.stage_name != L5A_STAGE_NAME
        or receipt.stage_contract_version != L5A_STAGE_CONTRACT_VERSION
        or receipt.input_manifest_id != compiled.source.manifest.artifact_manifest_id
        or receipt.input_manifest_hash != compiled.source.manifest.manifest_hash
        or receipt.output_manifest_id != manifest.artifact_manifest_id
        or receipt.output_manifest_hash != manifest.manifest_hash
        or receipt.skip_key != compiled.fingerprint
        or dict(receipt.accepted_contract_versions) != L5A_ACCEPTED_VERSIONS
    ):
        return None
    expected_receipt_id = deterministic_contract_id(
        "stage-receipt",
        {
            "stage": "L5a",
            "fingerprint": compiled.fingerprint,
            "status": "succeeded",
            "fabric_calls": metrics.fabric_calls,
        },
    )
    expected_metrics_id = deterministic_contract_id(
        "stage-resource-metrics",
        {
            "stage": "L5a",
            "fingerprint": compiled.fingerprint,
            "fabric_calls": metrics.fabric_calls,
            "cache_hits": 0,
        },
    )
    if (
        receipt.stage_receipt_id != expected_receipt_id
        or receipt.attempt_count != 1
        or receipt.error_codes
        or metrics.stage_id != "L5"
        or metrics.stage_name != L5A_STAGE_NAME
        or metrics.resource_metrics_id != expected_metrics_id
        or metrics.budget_snapshot_hash != _budget_snapshot_hash()
        or metrics.network_request_bytes <= 0
        or metrics.network_response_bytes <= 0
        or len(receipt.remote_operation_refs) != metrics.fabric_calls + 1
        or not (
            L5A_INSPECT_CALLS
            + L5A_PUBLISH_CALLS
            + L5A_POST_PUBLISH_READ_BACK_CALLS
            <= metrics.fabric_calls
            <= L5A_MAX_SUCCESS_FABRIC_CALLS
        )
        or not (
            _rows_per_target(compiled) * len(L5A_TARGET_ORDER)
            <= metrics.fabric_rows_read
            <= _rows_per_target(compiled) * len(L5A_TARGET_ORDER) * 2
        )
        or metrics.fabric_rows_written
        != _rows_per_target(compiled) * len(L5A_TARGET_ORDER)
        or metrics.search_calls
        or metrics.search_documents_read
        or metrics.search_documents_written
        or metrics.max_observed_concurrency != 1
        or metrics.exceeded_dimensions
    ):
        return None
    try:
        validate_receipt_resources(receipt, metrics)
    except ValueError:
        return None
    try:
        publication_token = _receipt_publication_token(receipt)
        if (
            (run_root / "publication-crosswalks.json").read_bytes()
            != (canonical_json(compiled.crosswalks) + "\n").encode("utf-8")
            or (run_root / "access-policy.json").read_bytes()
            != (canonical_json(compiled.access_policy) + "\n").encode("utf-8")
            or (run_root / "governed-assets.json").read_bytes()
            != (canonical_json(compiled.governed_assets) + "\n").encode("utf-8")
            or (run_root / "output-manifest.json").read_bytes()
            != (canonical_json(manifest) + "\n").encode("utf-8")
            or (run_root / "resource-metrics.json").read_bytes()
            != (canonical_json(metrics) + "\n").encode("utf-8")
            or (run_root / "stage-receipt.json").read_bytes()
            != (canonical_json(receipt) + "\n").encode("utf-8")
        ):
            return None
        for kind in L5A_TARGET_ORDER:
            if (
                (run_root / "definitions" / f"{kind}.json").read_bytes()
                != (canonical_json(compiled.definitions[kind]) + "\n").encode(
                    "utf-8"
                )
            ):
                return None
        table_paths = {
            table_id: run_root / _safe_table_file(table_id, ordinal)
            for ordinal, table_id in enumerate(sorted(compiled.tables))
        }
        for table_id, expected_table in compiled.tables.items():
            if _table_snapshot(
                table_id,
                pq.read_table(table_paths[table_id]),
            ) != _table_snapshot(table_id, expected_table):
                return None
        expected_states = {
            kind: _expected_state(
                compiled,
                kind,
                publication_token=publication_token,
            )
            for kind in L5A_TARGET_ORDER
        }
        expected_equivalences = _equivalences(compiled, expected_states)
        if equivalences != expected_equivalences:
            return None
        expected_manifest = _output_manifest(
            compiled,
            run_root,
            expected_equivalences,
            {
                kind: run_root / "definitions" / f"{kind}.json"
                for kind in L5A_TARGET_ORDER
            },
            table_paths,
        )
        if manifest != expected_manifest:
            return None
    except (OSError, ValueError, L5aPublicationError):
        return None
    expected_files = {
        _artifact_path(entry, run_root).relative_to(run_root)
        for entry in manifest.entries
    } | _FIXED_FILES
    if _persisted_files(run_root) != expected_files:
        return None
    for entry in manifest.entries:
        try:
            payload = _artifact_path(entry, run_root).read_bytes()
        except (OSError, L5aPublicationError):
            return None
        if (
            len(payload) != entry.byte_count
            or hashlib.sha256(payload).hexdigest() != entry.content_hash
        ):
            return None
    return manifest, metrics, receipt, equivalences


def _publish_atomic(temp_root: Path, run_root: Path) -> None:
    run_root.parent.mkdir(parents=True, exist_ok=True)
    replacement = run_root.with_name(f"{run_root.name}.replaced")
    if replacement.exists():
        shutil.rmtree(replacement)
    if run_root.exists():
        run_root.replace(replacement)
    try:
        temp_root.replace(run_root)
    except OSError:
        if replacement.exists() and not run_root.exists():
            replacement.replace(run_root)
        raise
    if replacement.exists():
        shutil.rmtree(replacement)


def _rows_per_target(compiled: L5aCompiledPublication) -> int:
    return sum(item.row_count for item in compiled.table_snapshots)


def _receipt_publication_token(receipt: StageReceipt) -> str:
    tokens = [
        value.removeprefix("publication-token:")
        for value in receipt.remote_operation_refs
        if value.startswith("publication-token:")
    ]
    if len(tokens) != 1 or not tokens[0]:
        raise L5aPublicationError(
            "L5A_PUBLICATION_TOKEN_MISSING",
            "successful publication receipt has no unique ownership token",
        )
    return tokens[0]


def run_l5a(
    source: SealedL4ServingSource,
    *,
    crosswalks: Sequence[PublicationCrosswalk],
    access_policy: AccessPolicy,
    governed_assets: Sequence[GovernedAssetReference],
    target_ids: Mapping[L5ATargetKind, str],
    client: L5aTargetClient,
    state_root: Path = L5A_STATE_DIR,
) -> L5aStageResult:
    """Persist, publish, and read back all four L5a structured targets."""

    started = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc)
    compiled = compile_l5a_publication(
        source,
        crosswalks=crosswalks,
        access_policy=access_policy,
        governed_assets=governed_assets,
        target_ids=target_ids,
    )
    run_root = state_root / "runs" / compiled.fingerprint
    accounting = _CallAccounting()
    published_target_count = 0
    restored_target_count = 0
    read_back_target_count = 0
    existing = _existing_is_intact(compiled, run_root)
    if existing is not None:
        manifest, _prior_metrics, prior_receipt, equivalences = existing
        try:
            prior_token = _receipt_publication_token(prior_receipt)
            states: dict[L5ATargetKind, L5aTargetState] = {}
            for kind in L5A_TARGET_ORDER:
                state = _invoke_state_operation(
                    accounting,
                    f"reuse-read-back:{kind}",
                    lambda kind=kind: client.read_back(
                        kind,
                        compiled.target_ids[kind],
                    ),
                )
                if state is not None:
                    read_back_target_count += 1
                _validate_state(
                    state,
                    _expected_state(
                        compiled,
                        kind,
                        publication_token=prior_token,
                    ),
                    phase="reuse",
                )
                states[kind] = state  # type: ignore[assignment]
            accounting.require_complete_references()
        except (
            L5aPublicationError,
            RuntimeError,
            PermissionError,
            TimeoutError,
            OSError,
            ValueError,
            TypeError,
            AttributeError,
        ):
            existing = None
        else:
            metrics = _metrics(
                compiled,
                started=started,
                accounting=accounting,
                storage_write_bytes=0,
                fabric_rows_read=(
                    _rows_per_target(compiled) * read_back_target_count
                ),
                fabric_rows_written=0,
                cache_hits=1,
            )
            receipt = _receipt(
                compiled,
                status="skipped",
                output_manifest=manifest,
                metrics=metrics,
                accounting=accounting,
                started_at_utc=started_at_utc,
            )
            return L5aStageResult(
                compiled=compiled,
                projection_equivalences=equivalences,
                output_manifest=manifest,
                metrics=metrics,
                receipt=receipt,
                run_root=run_root,
                reused=True,
            )

    state_root.mkdir(parents=True, exist_ok=True)
    publication_token = uuid.uuid4().hex
    accounting.remote_operation_refs = tuple(sorted({
        *accounting.remote_operation_refs,
        f"publication-token:{publication_token}",
    }))
    temp_root = Path(tempfile.mkdtemp(
        prefix=f".l5a-{compiled.fingerprint[:12]}-",
        dir=state_root,
    ))
    created: list[tuple[L5ATargetKind, str]] = []
    updated: list[tuple[L5ATargetKind, str, L5aTargetState]] = []
    publish_started: list[L5ATargetKind] = []
    publish_completed: set[L5ATargetKind] = set()
    prior_states: dict[L5ATargetKind, L5aTargetState | None] = {}
    try:
        definition_paths, table_paths = _persist_compiled(compiled, temp_root)
        for kind in L5A_TARGET_ORDER:
            prior = _invoke_state_operation(
                accounting,
                f"inspect:{kind}",
                lambda kind=kind: client.inspect(
                    kind,
                    compiled.target_ids[kind],
                ),
            )
            if prior is not None and prior.target_version != L5A_TARGET_VERSION:
                raise L5aPublicationError(
                    "L5A_TARGET_VERSION_UNSUPPORTED",
                    f"{kind} has unsupported version {prior.target_version!r}",
                )
            prior_states[kind] = prior

        for kind in L5A_TARGET_ORDER:
            if not definition_paths[kind].is_file() or any(
                not path.is_file() for path in table_paths.values()
            ):
                raise L5aPublicationError(
                    "L5A_MATERIALIZED_DEFINITION_MISSING",
                    f"{kind} publication inputs are not persisted",
                )
            publish_started.append(kind)
            operation = _invoke_mutation_operation(
                accounting,
                f"publish:{kind}",
                lambda kind=kind: client.publish(
                    kind,
                    compiled.target_ids[kind],
                    definition_path=definition_paths[kind],
                    table_paths=table_paths,
                    access_policy=access_policy,
                    expected_state=prior_states[kind],
                    publication_token=publication_token,
                ),
            )
            if (
                operation.target_kind != kind
                or operation.target_id != compiled.target_ids[kind]
                or operation.publication_token != publication_token
                or not operation.applied
                or operation.created != (prior_states[kind] is None)
            ):
                raise L5aPublicationError(
                    "L5A_DEPLOY_OPERATION_MISMATCH",
                    f"{kind} publish operation identifies another target",
                )
            if prior_states[kind] is None:
                created.append((kind, operation.target_id))
            else:
                updated.append((
                    kind,
                    operation.target_id,
                    prior_states[kind],  # type: ignore[arg-type]
                ))
            publish_completed.add(kind)
            published_target_count += 1

        states: dict[L5ATargetKind, L5aTargetState] = {}
        for kind in L5A_TARGET_ORDER:
            state = _invoke_state_operation(
                accounting,
                f"post-publish-read-back:{kind}",
                lambda kind=kind: client.read_back(
                    kind,
                    compiled.target_ids[kind],
                ),
            )
            if state is not None:
                read_back_target_count += 1
            _validate_state(
                state,
                _expected_state(
                    compiled,
                    kind,
                    publication_token=publication_token,
                ),
                phase="post-publish read-back",
            )
            states[kind] = state  # type: ignore[assignment]

        accounting.require_complete_references()
        if accounting.remote_error_codes:
            raise L5aPublicationError(
                "L5A_REMOTE_ACCOUNTING_INVALID",
                "successful publication cannot follow unaccounted remote activity",
            )
        if accounting.fabric_calls > L5A_MAX_SUCCESS_FABRIC_CALLS:
            raise L5aPublicationError(
                "L5A_CALL_BUDGET_EXCEEDED",
                "successful publication exceeded its state-machine call bound",
            )
        equivalences = _equivalences(compiled, states)
        _write_json(temp_root / "projection-equivalence.json", equivalences)
        output_manifest = _output_manifest(
            compiled,
            temp_root,
            equivalences,
            definition_paths,
            table_paths,
        )
        manifest_bytes = _write_json(
            temp_root / "output-manifest.json",
            output_manifest,
        )
        storage_write_bytes = sum(
            path.stat().st_size
            for path in temp_root.rglob("*")
            if path.is_file()
        )
        metrics = _metrics(
            compiled,
            started=started,
            accounting=accounting,
            storage_write_bytes=storage_write_bytes,
            fabric_rows_read=(
                _rows_per_target(compiled) * read_back_target_count
            ),
            fabric_rows_written=(
                _rows_per_target(compiled) * published_target_count
            ),
            cache_hits=0,
        )
        receipt = _receipt(
            compiled,
            status="succeeded",
            output_manifest=output_manifest,
            metrics=metrics,
            accounting=accounting,
            started_at_utc=started_at_utc,
        )
        _write_json(temp_root / "resource-metrics.json", metrics)
        _write_json(temp_root / "stage-receipt.json", receipt)
        # Metrics count the final manifest payload but not their own receipt files.
        if metrics.storage_write_bytes < len(manifest_bytes):
            raise L5aPublicationError(
                "L5A_RESOURCE_METRICS_INVALID",
                "storage metrics do not include the persisted manifest",
            )
        _publish_atomic(temp_root, run_root)
        return L5aStageResult(
            compiled=compiled,
            projection_equivalences=equivalences,
            output_manifest=output_manifest,
            metrics=metrics,
            receipt=receipt,
            run_root=run_root,
            reused=False,
        )
    except Exception as exc:
        cleanup_errors: list[str] = []
        for kind, target_id, prior_state in reversed(updated):
            try:
                operation = _invoke_mutation_operation(
                    accounting,
                    f"restore:{kind}",
                    lambda kind=kind, target_id=target_id, prior_state=prior_state: (
                        client.restore(
                            kind,
                            target_id,
                            prior_state=prior_state,
                            publication_token=publication_token,
                        )
                    ),
                )
                if (
                    operation.target_kind != kind
                    or operation.target_id != target_id
                    or operation.publication_token != publication_token
                    or not operation.applied
                ):
                    raise L5aPublicationError(
                        "L5A_RESTORE_OWNERSHIP_MISMATCH",
                        f"{kind} restore did not preserve target ownership fencing",
                    )
                restored_target_count += 1
            except Exception as restore_exc:
                cleanup_errors.append(f"{kind}:restore:{restore_exc}")
        recorded_created = set(created)
        for kind in publish_started:
            target_id = compiled.target_ids[kind]
            if (
                kind in publish_completed
                or (kind, target_id) in recorded_created
            ):
                continue
            try:
                recovered = _invoke_state_operation(
                    accounting,
                    f"recovery-inspect:{kind}",
                    lambda kind=kind, target_id=target_id: client.inspect(
                        kind,
                        target_id,
                    ),
                )
                if (
                    recovered is not None
                    and recovered.publication_token == publication_token
                ):
                    prior_state = prior_states.get(kind)
                    if prior_state is None:
                        created.append((kind, target_id))
                        recorded_created.add((kind, target_id))
                        published_target_count += 1
                    else:
                        published_target_count += 1
                        operation = _invoke_mutation_operation(
                            accounting,
                            f"recovery-restore:{kind}",
                            lambda kind=kind, target_id=target_id, prior_state=prior_state: (
                                client.restore(
                                    kind,
                                    target_id,
                                    prior_state=prior_state,
                                    publication_token=publication_token,
                                )
                            ),
                        )
                        if (
                            operation.target_kind != kind
                            or operation.target_id != target_id
                            or operation.publication_token != publication_token
                            or not operation.applied
                        ):
                            raise L5aPublicationError(
                                "L5A_RESTORE_OWNERSHIP_MISMATCH",
                                f"{kind} recovery restore did not preserve "
                                "target ownership fencing",
                            )
                        restored_target_count += 1
            except Exception as recovery_exc:
                cleanup_errors.append(f"{kind}:recovery:{recovery_exc}")
        for kind, target_id in reversed(created):
            try:
                operation = _invoke_mutation_operation(
                    accounting,
                    f"cleanup:{kind}",
                    lambda kind=kind, target_id=target_id: client.cleanup(
                        kind,
                        target_id,
                        publication_token=publication_token,
                    ),
                )
                if (
                    operation.target_kind != kind
                    or operation.target_id != target_id
                    or operation.publication_token != publication_token
                    or not operation.applied
                ):
                    raise L5aPublicationError(
                        "L5A_CLEANUP_OWNERSHIP_MISMATCH",
                        f"{kind} cleanup did not preserve target ownership fencing",
                    )
            except Exception as cleanup_exc:
                cleanup_errors.append(f"{kind}:{cleanup_exc}")
        storage_write_bytes = sum(
            path.stat().st_size
            for path in temp_root.rglob("*")
            if path.is_file()
        )
        metrics = _metrics(
            compiled,
            started=started,
            accounting=accounting,
            storage_write_bytes=storage_write_bytes,
            fabric_rows_read=(
                _rows_per_target(compiled) * read_back_target_count
            ),
            fabric_rows_written=(
                _rows_per_target(compiled)
                * (published_target_count + restored_target_count)
            ),
            cache_hits=0,
        )
        code = (
            exc.code
            if isinstance(exc, L5aPublicationError)
            else "L5A_PUBLICATION_FAILED"
        )
        error_codes = [code, *accounting.remote_error_codes]
        if cleanup_errors:
            error_codes.append("L5A_PARTIAL_CLEANUP_FAILED")
        receipt = _receipt(
            compiled,
            status="failed",
            output_manifest=None,
            metrics=metrics,
            accounting=accounting,
            started_at_utc=started_at_utc,
            error_codes=error_codes,
        )
        failure_root = state_root / "failures" / compiled.fingerprint
        failure_root.mkdir(parents=True, exist_ok=True)
        _write_json(failure_root / "resource-metrics.json", metrics)
        _write_json(failure_root / "stage-receipt.json", receipt)
        shutil.rmtree(temp_root, ignore_errors=True)
        message = str(exc)
        if cleanup_errors:
            message += f"; cleanup failures: {cleanup_errors}"
        raise L5aPublicationError(
            code,
            message,
            receipt=receipt,
            metrics=metrics,
        ) from exc


def require_l5a_publication_receipt(
    source: SealedL4ServingSource,
    result: L5aStageResult,
    *,
    client: L5aTargetClient | None = None,
) -> None:
    """Authorize schema-2 readiness only for an intact successful L5a result."""

    del client  # Read-back was already accounted in the succeeded/skipped stage.
    intact = _existing_is_intact(result.compiled, result.run_root)
    if intact is None:
        raise L5aPublicationError(
            "L5A_PUBLICATION_RECEIPT_INVALID",
            "schema-2 product readiness requires intact persisted L5a artifacts",
        )
    persisted_token = _receipt_publication_token(intact[2])
    expected_states = {
        kind: _expected_state(
            result.compiled,
            kind,
            publication_token=persisted_token,
        )
        for kind in L5A_TARGET_ORDER
    }
    expected_equivalences = _equivalences(result.compiled, expected_states)
    expected_proof_keys = {
        (
            crosswalk.authority.required_member_manifest_id,
            kind,
            crosswalk.publication_crosswalk_id,
            crosswalk.crosswalk_hash,
        )
        for crosswalk in result.compiled.crosswalks
        for kind in L5A_TARGET_ORDER
    }
    actual_proof_keys = {
        (
            proof.authority.required_member_manifest_id,
            proof.projection_kind,
            proof.publication_crosswalk_id,
            proof.publication_crosswalk_hash,
        )
        for proof in result.projection_equivalences
    }
    persisted_receipt_matches = (
        intact is not None
        and result.receipt == intact[2]
        and result.metrics == intact[1]
    )
    expected_skip_metrics_id = deterministic_contract_id(
        "stage-resource-metrics",
        {
            "stage": "L5a",
            "fingerprint": result.compiled.fingerprint,
            "fabric_calls": 4,
            "cache_hits": 1,
        },
    )
    expected_skip_receipt_id = deterministic_contract_id(
        "stage-receipt",
        {
            "stage": "L5a",
            "fingerprint": result.compiled.fingerprint,
            "status": "skipped",
            "fabric_calls": 4,
        },
    )
    skip_receipt_matches = (
        result.receipt.status == "skipped"
        and result.receipt.stage_receipt_id == expected_skip_receipt_id
        and result.receipt.identity
        == _identity(
            result.compiled.source,
            contract_kind="c0.stage_receipt",
        )
        and result.receipt.attempt_count == 1
        and len(result.receipt.remote_operation_refs)
        == L5A_REUSE_READ_BACK_CALLS
        and not any(
            value.startswith("publication-token:")
            for value in result.receipt.remote_operation_refs
        )
        and not result.receipt.error_codes
        and result.metrics.identity
        == _identity(
            result.compiled.source,
            contract_kind="c0.stage_resource_metrics",
        )
        and result.metrics.resource_metrics_id == expected_skip_metrics_id
        and result.metrics.stage_id == "L5"
        and result.metrics.stage_name == L5A_STAGE_NAME
        and result.metrics.storage_read_bytes
        == result.compiled.source.manifest.total_byte_count
        and result.metrics.storage_write_bytes == 0
        and result.metrics.network_request_bytes > 0
        and result.metrics.network_response_bytes > 0
        and result.metrics.fabric_calls == L5A_REUSE_READ_BACK_CALLS
        and result.metrics.fabric_rows_read
        == _rows_per_target(result.compiled) * len(L5A_TARGET_ORDER)
        and result.metrics.fabric_rows_written == 0
        and result.metrics.search_calls == 0
        and result.metrics.search_documents_read == 0
        and result.metrics.search_documents_written == 0
        and result.metrics.cache_hits == 1
        and result.metrics.cache_misses == 0
        and result.metrics.max_observed_concurrency == 1
        and not result.metrics.exceeded_dimensions
        and result.metrics.budget_snapshot_hash == _budget_snapshot_hash()
    )
    if (
        intact is None
        or intact[0] != result.output_manifest
        or intact[3] != expected_equivalences
        or result.compiled.source.receipt.receipt_hash
        != source.receipt.receipt_hash
        or result.receipt.stage_id != "L5"
        or result.receipt.stage_name != L5A_STAGE_NAME
        or result.receipt.stage_contract_version != L5A_STAGE_CONTRACT_VERSION
        or result.receipt.status not in {"succeeded", "skipped"}
        or result.receipt.skip_key != result.compiled.fingerprint
        or dict(result.receipt.accepted_contract_versions)
        != L5A_ACCEPTED_VERSIONS
        or result.receipt.input_manifest_id != source.manifest.artifact_manifest_id
        or result.receipt.input_manifest_hash != source.manifest.manifest_hash
        or result.receipt.output_manifest_id
        != result.output_manifest.artifact_manifest_id
        or result.receipt.output_manifest_hash != result.output_manifest.manifest_hash
        or result.projection_equivalences != expected_equivalences
        or actual_proof_keys != expected_proof_keys
        or not (persisted_receipt_matches or skip_receipt_matches)
    ):
        raise L5aPublicationError(
            "L5A_PUBLICATION_RECEIPT_INVALID",
            "schema-2 product readiness requires an exact L5a publication receipt",
        )
    try:
        validate_receipt_resources(result.receipt, result.metrics)
    except ValueError as exc:
        raise L5aPublicationError(
            "L5A_PUBLICATION_RECEIPT_INVALID",
            str(exc),
        ) from exc
