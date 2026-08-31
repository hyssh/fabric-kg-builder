"""Schema-2 L5b evidence publication and zero-synthesis Search retrieval.

The preview payload shapes follow the first-party Azure AI Search guidance:

* https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-migrate
* https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-retrieve
* https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-search-index

This module is an interoperability boundary, not an answer engine. It publishes
sealed evidence data and returns citations plus structural coverage only.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence
from urllib.parse import unquote, urlparse

from fabric_kg_builder.contracts.base import (
    canonical_json,
    canonical_sha256,
    deterministic_contract_id,
    normalize_nfc,
    reject_secret_text,
)
from fabric_kg_builder.contracts.evidence import EvidenceSpanV1_1, SourceUnit
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope
from fabric_kg_builder.contracts.identity import ImmutableSourceLocator
from fabric_kg_builder.contracts.publication import (
    AccessPolicy,
    GovernedAssetReference,
    ProjectionEquivalenceIdentityV1_1,
    ProjectionEquivalenceV1_1,
    ProjectionEvidence,
)
from fabric_kg_builder.contracts.receipts import ArtifactEntry, ArtifactManifest, StageReceipt
from fabric_kg_builder.contracts.resources import StageResourceMetrics
from fabric_kg_builder.contracts.resources import validate_receipt_resources
from fabric_kg_builder.contracts.runtime import (
    ActivityReceipt,
    AgenticRetrievalCoverageReceiptV1_1,
    AgenticRetrievalCoverageReceiptIdentityV1_1,
    AgenticRetrievalRequestContextV1_1,
    CitationCanonicalMapping,
    CitationPresentation,
    CoverageBudgetObservationV1_1,
    CoverageMemberReference,
    PlannedSubqueryReceipt,
    QueryBudgetV1_1,
    ResolvedOntologyScope,
    ResolvedRetrievalScope,
    RetrievalFailure,
    SearchCitationEnvelope,
    SourceCallReceipt,
)
from fabric_kg_builder.platform import process_resource_usage
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
from fabric_kg_builder.serving.structured_publication import (
    L5aStageResult,
    require_l5a_publication_receipt,
)

L5B_STAGE_NAME = "schema2-evidence-retrieval-publication"
L5B_STAGE_CONTRACT_VERSION = "1.0.0"
L5B_PUBLICATION_CODE_VERSION = "0.2.3/l5b-1"
L5B_STATE_DIR = Path(".fkg") / "l5b"
L5B_INDEX_API_VERSION = "2026-04-01"
L5B_AGENTIC_API_VERSION = "2026-05-01-preview"
L5B_TARGET_VERSION = "1.0.0"
L5B_MAX_BATCH_SIZE = 1000

L5B_REUSE_READ_BACK_CALLS = 1
L5B_INSPECT_CALLS = 1
L5B_PUBLISH_CALLS = 1
L5B_POST_PUBLISH_READ_BACK_CALLS = 1
L5B_ROLLBACK_MUTATION_CALLS = 1
L5B_AMBIGUOUS_RECOVERY_INSPECT_CALLS = 1
L5B_MAX_SUCCESS_SEARCH_CALLS = 4
L5B_MAX_SEARCH_CALLS = 5

L5B_ACCEPTED_VERSIONS = {
    "c0.access_policy": "1.0.0",
    "c0.artifact_manifest": "1.0.0",
    "c0.evidence_span": "1.1.0",
    "c0.governed_asset_reference": "1.0.0",
    "c0.projection_equivalence": "1.0.0",
    "c0.publication_crosswalk": "1.1.0",
    "c0.source_unit": "1.0.0",
    "c0.stage_receipt": "1.0.0",
    "c0.stage_resource_metrics": "1.0.0",
}

_ASSERTION_TABLES = (
    ("l4_semantic_asserted_entities", "entity", "entity_id", "evidence_span_ids"),
    (
        "l4_semantic_asserted_relationships",
        "relationship",
        "relationship_id",
        "evidence_span_ids",
    ),
    (
        "l4_semantic_asserted_properties",
        "property",
        "property_assertion_id",
        "evidence_span_ids",
    ),
    (
        "l4_semantic_required_members",
        "required_member",
        "candidate_id",
        "supporting_evidence_span_ids",
    ),
)
_SOURCE_DATA_FIELDS = (
    "id",
    "document_hash",
    "assertion_kind",
    "canonical_entity_ids",
    "canonical_relationship_ids",
    "canonical_property_ids",
    "canonical_type_ids",
    "canonical_assertion_ids",
    "required_member_manifest_ids",
    "source_id",
    "original_document_name",
    "source_file_id",
    "asset_id",
    "asset_version_id",
    "asset_hash",
    "source_unit_id",
    "source_unit_hash",
    "source_unit_kind",
    "source_text_content_hash",
    "evidence_span_ids",
    "evidence_span_hashes",
    "evidence_purposes",
    "content",
    "source_quote",
    "source_quote_is_verbatim",
    "quote_hash",
    "immutable_locator_json",
    "immutable_locator_hash",
    "page",
    "section_path",
    "access_policy_id",
    "access_policy_hash",
    "acl_principal_keys",
    "acl_scope_keys",
    "authorization_resource_id",
    "l3_artifact_manifest_id",
    "l3_artifact_manifest_hash",
    "l4_projection_hash",
    "l4_receipt_hash",
    "l5a_publication_fingerprint",
    "l5a_receipt_hash",
    "publication_crosswalk_hashes",
    "asserted_publication_hash",
    "lifecycle_state",
    "governed_asset_reference_id",
    "governed_asset_reference_hash",
    "vector",
    "vector_state",
)


class L5bPublicationError(RuntimeError):
    """Fail-closed L5b error with optional persisted failure evidence."""

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


L5B_CHECKPOINT_SIGNING_ALGORITHM = "HMAC-SHA256"


@dataclass(frozen=True)
class CheckpointSignerIdentity:
    key_id: str
    key_version: str
    algorithm: str


class CheckpointIntegritySigner(Protocol):
    """Opaque protected signer; secret key material never enters fabric-kg."""

    @property
    def key_id(self) -> str: ...

    @property
    def key_version(self) -> str: ...

    @property
    def algorithm(self) -> str: ...

    def sign(self, canonical_payload: bytes) -> str: ...

    def verify(self, canonical_payload: bytes, persisted_mac: str) -> bool: ...


@dataclass(frozen=True)
class _ResolvedCheckpointSigner:
    signer: CheckpointIntegritySigner = field(repr=False)
    identity: CheckpointSignerIdentity


def _resolve_checkpoint_signer(
    signer: CheckpointIntegritySigner | None,
) -> _ResolvedCheckpointSigner | None:
    if signer is None:
        return None
    try:
        identity = CheckpointSignerIdentity(
            key_id=signer.key_id,
            key_version=signer.key_version,
            algorithm=signer.algorithm,
        )
    except Exception:
        return None
    for field_name, value in (
        ("key_id", identity.key_id),
        ("key_version", identity.key_version),
    ):
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or len(value) > 256
            or value != normalize_nfc(value)
            or urlparse(value).scheme
            or "://" in value
        ):
            raise L5bPublicationError(
                "L5B_CHECKPOINT_KEY_INVALID",
                f"checkpoint {field_name} is malformed",
            )
        try:
            reject_secret_text(value, field_name=field_name)
        except ValueError as exc:
            raise L5bPublicationError(
                "L5B_CHECKPOINT_KEY_INVALID",
                str(exc),
            ) from exc
    if identity.key_id.casefold() in {
        "default",
        "changeme",
        "none",
        "unknown",
        "test",
    }:
        raise L5bPublicationError(
            "L5B_CHECKPOINT_KEY_INVALID",
            "checkpoint key ID must identify protected non-default material",
        )
    if identity.algorithm != L5B_CHECKPOINT_SIGNING_ALGORITHM:
        raise L5bPublicationError(
            "L5B_CHECKPOINT_SIGNER_UNSUPPORTED",
            f"unsupported checkpoint signing algorithm {identity.algorithm!r}",
        )
    return _ResolvedCheckpointSigner(signer=signer, identity=identity)


def _sign_checkpoint_payload(
    signer: _ResolvedCheckpointSigner,
    payload: Mapping[str, Any],
) -> str:
    canonical_payload = canonical_json(payload).encode("utf-8")
    try:
        mac = signer.signer.sign(canonical_payload)
    except Exception as exc:
        raise L5bPublicationError(
            "L5B_CHECKPOINT_SIGNING_FAILED",
            "checkpoint signer could not sign canonical payload",
        ) from exc
    if (
        not isinstance(mac, str)
        or re.fullmatch(r"[0-9a-f]{64}", mac) is None
    ):
        raise L5bPublicationError(
            "L5B_CHECKPOINT_MAC_INVALID",
            "checkpoint signer returned a malformed MAC",
        )
    return mac


def _verify_checkpoint_payload(
    signer: _ResolvedCheckpointSigner,
    payload: Mapping[str, Any],
    persisted_mac: object,
) -> bool:
    if (
        not isinstance(persisted_mac, str)
        or re.fullmatch(r"[0-9a-f]{64}", persisted_mac) is None
    ):
        return False
    canonical_payload = canonical_json(payload).encode("utf-8")
    try:
        verified = signer.signer.verify(canonical_payload, persisted_mac)
    except Exception:
        return False
    return verified is True


@dataclass(frozen=True)
class L5bRemoteAccounting:
    operation_refs: tuple[str, ...]
    request_bytes: int
    response_bytes: int
    retry_count: int
    retry_wait_ms: int
    latency_ms: int
    candidate_count: int = 0
    vector_search_requests: int = 0
    embedding_calls: int = 0
    embedding_items: int = 0
    output_tokens: int | None = None
    truncated: bool = False
    warning_codes: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class L5bTargetState:
    target_id: str
    target_version: str
    index_definition: Mapping[str, Any]
    knowledge_source_definition: Mapping[str, Any]
    knowledge_base_definition: Mapping[str, Any]
    document_ids: tuple[str, ...]
    document_hashes: tuple[tuple[str, str], ...]
    vector_state_hash: str
    access_policy_id: str
    access_policy_hash: str
    publication_token: str


@dataclass(frozen=True)
class L5bStateOperation:
    state: L5bTargetState | None
    accounting: L5bRemoteAccounting


@dataclass(frozen=True)
class L5bMutationOperation:
    target_id: str
    created: bool
    applied: bool
    publication_token: str
    accounting: L5bRemoteAccounting


class L5bTargetClient(Protocol):
    """Bounded Search publication adapter; live validation remains L7."""

    def inspect(self, target_id: str) -> L5bStateOperation: ...

    def publish(
        self,
        target_id: str,
        *,
        index_definition_path: Path,
        knowledge_source_definition_path: Path,
        knowledge_base_definition_path: Path,
        documents_path: Path,
        batch_size: int,
        expected_state: L5bTargetState | None,
        publication_token: str,
    ) -> L5bMutationOperation: ...

    def read_back(self, target_id: str) -> L5bStateOperation: ...

    def cleanup(
        self,
        target_id: str,
        *,
        publication_token: str,
    ) -> L5bMutationOperation: ...

    def restore(
        self,
        target_id: str,
        *,
        prior_state: L5bTargetState,
        publication_token: str,
    ) -> L5bMutationOperation: ...


@dataclass(frozen=True)
class L5bCompiledPublication:
    source: SealedL4ServingSource
    l5a_result: L5aStageResult
    fingerprint: str
    target_id: str
    index_name: str
    knowledge_source_name: str
    knowledge_base_name: str
    index_definition: Mapping[str, Any]
    knowledge_source_definition: Mapping[str, Any]
    knowledge_base_definition: Mapping[str, Any]
    documents: tuple[Mapping[str, Any], ...]
    document_ids: tuple[str, ...]
    document_hashes: tuple[tuple[str, str], ...]
    vector_state_hash: str
    index_fingerprint: str
    access_policy: AccessPolicy
    governed_assets: tuple[GovernedAssetReference, ...]


@dataclass(frozen=True)
class L5bStageResult:
    compiled: L5bCompiledPublication
    projection_equivalences: tuple[ProjectionEquivalenceV1_1, ...]
    output_manifest: ArtifactManifest
    metrics: StageResourceMetrics
    receipt: StageReceipt
    run_root: Path
    reused: bool


@dataclass
class _PublicationAccounting:
    search_calls: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    retry_count: int = 0
    retry_wait_ms: int = 0
    search_documents_read: int = 0
    search_documents_written: int = 0
    remote_operation_refs: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()

    def invoke(self, label: str, callback: Any) -> Any:
        if self.search_calls >= L5B_MAX_SEARCH_CALLS:
            raise L5bPublicationError(
                "L5B_CALL_BUDGET_EXCEEDED",
                f"remote call budget {L5B_MAX_SEARCH_CALLS} exhausted",
            )
        self.search_calls += 1
        try:
            operation = callback()
        except Exception:
            self.error_codes = tuple(sorted({
                *self.error_codes,
                f"L5B_REMOTE_{label.upper().replace('-', '_')}_AMBIGUOUS",
            }))
            raise
        accounting = getattr(operation, "accounting", None)
        _validate_remote_accounting(accounting)
        overlap = set(self.remote_operation_refs).intersection(accounting.operation_refs)
        if overlap:
            raise L5bPublicationError(
                "L5B_REMOTE_REFERENCE_REUSED",
                f"remote operation references were reused: {sorted(overlap)}",
            )
        self.request_bytes += accounting.request_bytes
        self.response_bytes += accounting.response_bytes
        self.retry_count += accounting.retry_count
        self.retry_wait_ms += accounting.retry_wait_ms
        self.remote_operation_refs = tuple(sorted({
            *self.remote_operation_refs,
            *accounting.operation_refs,
        }))
        self.warning_codes = tuple(sorted({
            *self.warning_codes,
            *accounting.warning_codes,
        }))
        self.error_codes = tuple(sorted({
            *self.error_codes,
            *accounting.error_codes,
        }))
        if accounting.error_codes:
            raise L5bPublicationError(
                "L5B_REMOTE_OPERATION_FAILED",
                f"remote operation reported errors {accounting.error_codes}",
            )
        return operation

    def require_complete(self) -> None:
        refs = [
            item
            for item in self.remote_operation_refs
            if not item.startswith("publication-token:")
        ]
        if len(refs) != self.search_calls:
            raise L5bPublicationError(
                "L5B_REMOTE_REFERENCE_COUNT_MISMATCH",
                f"{self.search_calls} calls produced {len(refs)} unique references",
            )


def _validate_remote_accounting(value: object) -> None:
    if not isinstance(value, L5bRemoteAccounting):
        raise L5bPublicationError(
            "L5B_REMOTE_ACCOUNTING_MISSING",
            "Search adapter omitted uniform remote accounting",
        )
    counters = (
        value.request_bytes,
        value.response_bytes,
        value.retry_count,
        value.retry_wait_ms,
        value.latency_ms,
        value.candidate_count,
        value.vector_search_requests,
        value.embedding_calls,
        value.embedding_items,
    )
    if (
        not value.operation_refs
        or value.operation_refs != tuple(sorted(set(value.operation_refs)))
        or value.warning_codes != tuple(sorted(set(value.warning_codes)))
        or value.error_codes != tuple(sorted(set(value.error_codes)))
        or any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in counters)
        or (
            value.output_tokens is not None
            and (
                not isinstance(value.output_tokens, int)
                or isinstance(value.output_tokens, bool)
                or value.output_tokens < 0
            )
        )
        or value.request_bytes <= 0
        or value.response_bytes <= 0
        or (value.retry_count == 0 and value.retry_wait_ms != 0)
        or ((value.embedding_calls == 0) != (value.embedding_items == 0))
        or value.embedding_items < value.embedding_calls
    ):
        raise L5bPublicationError(
            "L5B_REMOTE_ACCOUNTING_INVALID",
            "Search adapter returned malformed accounting",
        )


def _identity(source: SealedL4ServingSource, contract_kind: str) -> CanonicalIdentityEnvelope:
    values = source.receipt.identity.model_dump(mode="python", round_trip=True)
    values.update({
        "contract_kind": contract_kind,
        "contract_version": "1.0.0",
        "canonical_schema_version": "2.0",
        "parent_artifact_ids": tuple(sorted({
            *source.receipt.identity.parent_artifact_ids,
            source.input_manifest.artifact_manifest_id,
            source.manifest.artifact_manifest_id,
        })),
    })
    return CanonicalIdentityEnvelope.model_validate(values)


def _safe_document_id(seed: Mapping[str, Any]) -> str:
    digest = bytes.fromhex(canonical_sha256(seed))
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _principal_keys(policy: AccessPolicy) -> tuple[str, ...]:
    values = [
        f"{item.principal_type}:{item.principal_id}"
        for item in policy.principal_scopes
    ]
    if len(values) != len(set(values)):
        raise L5bPublicationError(
            "L5B_ACL_PRINCIPAL_COLLISION",
            "access policy contains colliding principal keys",
        )
    return tuple(sorted(values))


def _scope_keys(policy: AccessPolicy) -> tuple[str, ...]:
    owners: dict[str, str] = {}
    for principal in policy.principal_scopes:
        owner = f"{principal.principal_type}:{principal.principal_id}"
        for scope in principal.resource_scope_ids:
            prior = owners.setdefault(scope, owner)
            if prior != owner:
                raise L5bPublicationError(
                    "L5B_ACL_SCOPE_COLLISION",
                    f"resource scope {scope!r} is assigned to multiple principals",
                )
    return tuple(sorted(owners))


def _validate_evidence_partitions(
    source: SealedL4ServingSource,
    evidence_partitions: Mapping[str, Sequence[EvidenceSpanV1_1]],
) -> dict[str, EvidenceSpanV1_1]:
    entries = {
        entry.artifact_id: entry
        for entry in source.input_manifest.entries
        if entry.contract_kind == "c0.evidence_span"
    }
    if set(entries) != set(evidence_partitions):
        raise L5bPublicationError(
            "L5B_L3_EVIDENCE_PARTITION_MISMATCH",
            "evidence partitions must exactly equal anchored L3 manifest entries",
        )
    indexed: dict[str, EvidenceSpanV1_1] = {}
    schema_hash = canonical_sha256(EvidenceSpanV1_1.model_json_schema())
    for artifact_id, spans in sorted(evidence_partitions.items()):
        ordered = tuple(sorted(spans, key=lambda item: item.evidence_span_id))
        entry = entries[artifact_id]
        if (
            entry.contract_version != "1.1.0"
            or entry.schema_hash != schema_hash
            or entry.row_count != len(ordered)
            or entry.content_hash
            != canonical_sha256([item.model_dump(mode="json") for item in ordered])
            or entry.canonical_id_set_hash
            != canonical_sha256([item.evidence_span_id for item in ordered])
        ):
            raise L5bPublicationError(
                "L5B_L3_EVIDENCE_PARTITION_TAMPERED",
                f"evidence partition {artifact_id!r} differs from anchored L3 manifest",
            )
        for span in ordered:
            prior = indexed.setdefault(span.evidence_span_id, span)
            if prior != span:
                raise L5bPublicationError(
                    "L5B_EVIDENCE_ID_COLLISION",
                    f"conflicting evidence span {span.evidence_span_id}",
                )
    return indexed


def _validate_source_units(
    l5a: L5aStageResult,
    source_unit_manifest: ArtifactManifest,
    source_units: Sequence[SourceUnit],
) -> tuple[SourceUnit, ...]:
    authorities = {
        (
            str(row["source_unit_manifest_id"]),
            str(row["source_unit_manifest_hash"]),
        )
        for row in l5a.compiled.required_member_manifest_rows
    }
    expected = (
        source_unit_manifest.artifact_manifest_id,
        source_unit_manifest.manifest_hash,
    )
    if authorities != {expected}:
        raise L5bPublicationError(
            "L5B_SOURCE_UNIT_MANIFEST_STALE",
            "SourceUnit manifest differs from sealed required-member authority",
        )
    entries = {
        entry.artifact_id: entry for entry in source_unit_manifest.entries
    }
    units = {item.source_unit_id: item for item in source_units}
    if len(units) != len(source_units) or set(entries) != set(units):
        raise L5bPublicationError(
            "L5B_SOURCE_UNIT_MANIFEST_MISMATCH",
            "SourceUnit IDs must exactly equal their sealed manifest",
        )
    schema_hash = canonical_sha256(SourceUnit.model_json_schema())
    for source_unit_id, unit in units.items():
        entry = entries[source_unit_id]
        if (
            entry.contract_kind != "c0.source_unit"
            or entry.contract_version != "1.0.0"
            or entry.schema_hash != schema_hash
            or entry.content_hash != canonical_sha256(unit)
            or entry.row_count != 1
        ):
            raise L5bPublicationError(
                "L5B_SOURCE_UNIT_TAMPERED",
                f"SourceUnit {source_unit_id} differs from its sealed manifest",
            )
    return tuple(units[key] for key in sorted(units))


def _asset_by_source_file(
    assets: Sequence[GovernedAssetReference],
    policy: AccessPolicy,
) -> dict[str, GovernedAssetReference]:
    indexed: dict[str, GovernedAssetReference] = {}
    for asset in assets:
        asset.validate_access_policy(policy)
        prior = indexed.setdefault(asset.source_file_id, asset)
        if prior != asset:
            raise L5bPublicationError(
                "L5B_ASSET_SOURCE_COLLISION",
                f"multiple governed assets claim source file {asset.source_file_id}",
            )
    return indexed


def _validate_governed_asset_input(
    l5a: L5aStageResult,
    assets: Sequence[GovernedAssetReference],
    policy: AccessPolicy,
) -> tuple[GovernedAssetReference, ...]:
    ordered = tuple(sorted(
        assets,
        key=lambda item: item.governed_asset_reference_id,
    ))
    expected = l5a.compiled.governed_assets
    ids = [item.governed_asset_reference_id for item in assets]
    if (
        len(ids) != len(set(ids))
        or len(ordered) != len(expected)
        or ordered != expected
    ):
        raise L5bPublicationError(
            "L5B_GOVERNED_ASSET_SET_MISMATCH",
            "governed assets must exactly equal sealed L5a ID/hash/content authority",
        )
    for asset in ordered:
        try:
            asset.validate_access_policy(policy)
        except ValueError as exc:
            raise L5bPublicationError(
                "L5B_GOVERNED_ASSET_POLICY_MISMATCH",
                str(exc),
            ) from exc
        if (
            asset.identity.project_id != l5a.compiled.source.receipt.identity.project_id
            or asset.identity.domain_contract_hash
            != l5a.compiled.source.receipt.identity.domain_contract_hash
            or asset.identity.semantic_contract_hash
            != l5a.compiled.source.projection.sealed_semantic_contract_hash
        ):
            raise L5bPublicationError(
                "L5B_GOVERNED_ASSET_AUTHORITY_MISMATCH",
                f"governed asset {asset.governed_asset_reference_id} is stale",
            )
    return ordered


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_CONNECTION_STRING_MARKERS = (
    "defaultendpointsprotocol=",
    "accountname=",
    "accountkey=",
    "sharedaccesssignature=",
    "client_secret=",
    "connection string",
    "server=",
    "driver=",
    "password=",
    "pwd=",
    "user id=",
    "userid=",
    "uid=",
    "username=",
    "user=",
    "host=",
    "database=",
    "initial catalog=",
    "data source=",
    "dsn=",
    "port=",
)
_CONNECTION_STRING_KEY_RE = re.compile(
    r"(?i)(?:^|;)\s*(?:"
    r"defaultendpointsprotocol|accountname|accountkey|sharedaccesssignature|"
    r"client_secret|password|pwd|user\s*id|userid|uid|username|user|"
    r"host|database|initial\s+catalog|data\s+source|dsn|port|server|driver"
    r")\s*="
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:"
    r"api[\s_-]*key|access[\s_-]*key|account[\s_-]*key|"
    r"client[\s_-]*secret|"
    r"secret|password|passwd|pwd|token|credential|"
    r"connection[\s_-]*(?:string|str)|sas|sig|signature"
    r")\s*(?:=|:)"
)


def _credential_assignment_detected(value: str) -> bool:
    detection_value = unicodedata.normalize("NFKC", value).casefold()
    strong_stems = (
        "apikey",
        "accesskey",
        "accountkey",
        "clientsecret",
        "refreshtoken",
        "credential",
        "password",
        "passwd",
        "connectionstring",
        "connectionstr",
        "sharedaccesssignature",
    )
    contextual_stems = (
        "tokenvalue",
        "tokenkey",
        "tokensecret",
        "tokenname",
        "tokenlabel",
        "secretvalue",
        "secretkey",
        "secretname",
        "secretlabel",
        "sastoken",
        "saskey",
        "sigvalue",
        "sigkey",
        "signaturevalue",
        "signaturekey",
    )
    for index, char in enumerate(detection_value):
        if char not in "=:":
            continue
        key_segment = detection_value[:index]
        skeleton = "".join(
            item
            for item in key_segment
            if item.isalnum()
        )
        if (
            any(stem in skeleton for stem in strong_stems)
            or any(stem in skeleton for stem in contextual_stems)
            or skeleton.endswith(("token", "secret", "pwd", "sas", "sig", "signature"))
        ):
            return True
    return False


_UNSAFE_URI_SCHEMES = {
    "http",
    "https",
    "file",
    "data",
    "ftp",
    "ftps",
    "blob",
    "javascript",
}


def _unsafe_display_text(value: str, *, reject_any_scheme: bool) -> bool:
    parsed = urlparse(value)
    folded = value.casefold()
    return bool(
        (parsed.scheme and (reject_any_scheme or parsed.scheme.casefold() in _UNSAFE_URI_SCHEMES))
        or parsed.netloc
        or "://" in value
        or value.startswith(("/", "\\", "~"))
        or _WINDOWS_ABSOLUTE_PATH.match(value)
        or "/" in value
        or "\\" in value
        or "?" in value
        or "#" in value
        or any(marker in folded for marker in _CONNECTION_STRING_MARKERS)
        or _CONNECTION_STRING_KEY_RE.search(value)
        or _CREDENTIAL_ASSIGNMENT_RE.search(value)
        or _credential_assignment_detected(value)
    )


def _unsafe_unicode_character(char: str) -> bool:
    codepoint = ord(char)
    category = unicodedata.category(char)
    return (
        category in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        or 0xFDD0 <= codepoint <= 0xFDEF
        or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}
    )


def _safe_display_text(
    value: str,
    *,
    field_name: str,
    subject_id: str,
    reject_any_scheme: bool,
) -> str:
    if not isinstance(value, str):
        raise L5bPublicationError(
            "L5B_DISPLAY_TEXT_UNSAFE",
            f"{field_name} for {subject_id} must be text",
        )
    if value != normalize_nfc(value) or not value.strip() or value != value.strip():
        raise L5bPublicationError(
            "L5B_DISPLAY_TEXT_UNSAFE",
            f"{field_name} for {subject_id} must be nonempty NFC text",
        )
    try:
        reject_secret_text(value, field_name=field_name)
    except ValueError as exc:
        raise L5bPublicationError(
            "L5B_DISPLAY_TEXT_UNSAFE",
            str(exc),
        ) from exc
    decoded = value
    try:
        for _ in range(5):
            next_decoded = unquote(decoded, errors="strict")
            if next_decoded == decoded:
                break
            decoded = next_decoded
        else:
            if unquote(decoded, errors="strict") != decoded:
                raise L5bPublicationError(
                    "L5B_DISPLAY_TEXT_UNSAFE",
                    f"{field_name} for {subject_id} has excessive nested encoding",
                )
    except UnicodeDecodeError as exc:
        raise L5bPublicationError(
            "L5B_DISPLAY_TEXT_UNSAFE",
            f"{field_name} for {subject_id} contains invalid percent encoding",
        ) from exc
    try:
        reject_secret_text(decoded, field_name=field_name)
    except ValueError as exc:
        raise L5bPublicationError(
            "L5B_DISPLAY_TEXT_UNSAFE",
            str(exc),
        ) from exc
    if (
        any(_unsafe_unicode_character(char) for char in value)
        or any(_unsafe_unicode_character(char) for char in decoded)
        or _unsafe_display_text(value, reject_any_scheme=reject_any_scheme)
        or _unsafe_display_text(decoded, reject_any_scheme=reject_any_scheme)
    ):
        raise L5bPublicationError(
            "L5B_DISPLAY_TEXT_UNSAFE",
            f"{field_name} for {subject_id} must not contain a URL, path, locator, control, or credential",
        )
    return value


def _safe_source_display_name(value: str, *, source_file_id: str) -> str:
    try:
        return _safe_display_text(
            value,
            field_name="original_document_name",
            subject_id=source_file_id,
            reject_any_scheme=True,
        )
    except L5bPublicationError as exc:
        raise L5bPublicationError(
            "L5B_SOURCE_FILE_NAME_UNSAFE",
            str(exc),
        ) from exc


def _safe_section_path(
    value: Sequence[str],
    *,
    source_unit_id: str,
) -> tuple[str, ...]:
    return tuple(
        _safe_display_text(
            component,
            field_name=f"section_path[{index}]",
            subject_id=source_unit_id,
            reject_any_scheme=False,
        )
        for index, component in enumerate(value)
    )


def _manifest_ids_by_member(l5a: L5aStageResult) -> dict[str, tuple[str, ...]]:
    memberships: dict[str, set[str]] = {}
    for row in l5a.compiled.required_member_rows:
        memberships.setdefault(str(row["member_canonical_id"]), set()).add(
            str(row["required_member_manifest_id"])
        )
    return {key: tuple(sorted(value)) for key, value in memberships.items()}


def _membership_relationships_by_member(
    l5a: L5aStageResult,
) -> dict[str, tuple[str, ...]]:
    relationships: dict[str, set[str]] = {}
    for row in l5a.compiled.required_member_rows:
        relationships.setdefault(str(row["member_canonical_id"]), set()).add(
            str(row["membership_semantic_relationship_id"])
        )
    return {
        key: tuple(sorted(value)) for key, value in relationships.items()
    }


def _assertion_documents(
    source: SealedL4ServingSource,
    l5a: L5aStageResult,
    *,
    evidence: Mapping[str, EvidenceSpanV1_1],
    source_unit_manifest: ArtifactManifest,
    source_units: Sequence[SourceUnit],
    source_file_names: Mapping[str, str],
    policy: AccessPolicy,
    assets: Sequence[GovernedAssetReference],
) -> tuple[Mapping[str, Any], ...]:
    validated_units = _validate_source_units(
        l5a,
        source_unit_manifest,
        source_units,
    )
    units = {item.source_unit_id: item for item in validated_units}
    asset_by_file = _asset_by_source_file(assets, policy)
    manifest_ids_by_member = _manifest_ids_by_member(l5a)
    membership_relationships_by_member = _membership_relationships_by_member(l5a)
    principal_keys = _principal_keys(policy)
    scope_keys = _scope_keys(policy)
    entity_type_ids = {
        str(row["entity_id"]): tuple(
            sorted(str(item) for item in row["asserted_type_ids"])
        )
        for row in l5a.compiled.tables[
            "l4_semantic_asserted_entities"
        ].to_pylist()
    }
    crosswalk_hashes = tuple(
        sorted(item.crosswalk_hash for item in l5a.compiled.crosswalks)
    )
    documents: list[Mapping[str, Any]] = []
    expected_evidence = set(source.projection.evidence_span_ids)
    if not expected_evidence <= set(evidence):
        missing = sorted(expected_evidence - set(evidence))
        raise L5bPublicationError(
            "L5B_ASSERTED_EVIDENCE_MISSING",
            f"sealed L4 evidence is missing: {missing}",
        )

    for table_name, assertion_kind, id_field, evidence_field in _ASSERTION_TABLES:
        table = l5a.compiled.tables[table_name]
        for row in table.to_pylist():
            assertion_id = str(row[id_field])
            evidence_ids = tuple(sorted(str(item) for item in row[evidence_field]))
            if not evidence_ids:
                if assertion_kind == "required_member":
                    # L3 membership authority can legitimately carry no quote.
                    # It remains a coverage requirement, never an index document.
                    continue
                raise L5bPublicationError(
                    "L5B_ASSERTION_EVIDENCE_MISSING",
                    f"assertion {assertion_id} has no exact evidence",
                )
            entity_ids = tuple(sorted({
                str(value)
                for key in (
                    "entity_id",
                    "source_entity_id",
                    "target_entity_id",
                    "member_canonical_id",
                )
                for value in (row.get(key),)
                if value is not None
            }))
            relationship_ids = (
                (str(row["semantic_relationship_id"]),)
                if assertion_kind == "relationship"
                else (str(row["membership_semantic_relationship_id"]),)
                if assertion_kind == "required_member"
                else tuple(sorted({
                    relationship_id
                    for entity_id in entity_ids
                    for relationship_id in membership_relationships_by_member.get(
                        entity_id,
                        (),
                    )
                }))
            )
            property_ids = (
                (str(row["semantic_property_id"]),)
                if assertion_kind == "property"
                else ()
            )
            type_ids = tuple(sorted({
                str(value)
                for key in (
                    "most_specific_type_id",
                    "member_semantic_type_id",
                )
                for value in (row.get(key),)
                if value is not None
            } | {
                str(value) for value in (row.get("asserted_type_ids") or ())
            } | {
                type_id
                for key in ("source_entity_id", "target_entity_id")
                for entity_id in (row.get(key),)
                if entity_id is not None
                for type_id in entity_type_ids.get(str(entity_id), ())
            }))
            member_manifest_ids = tuple(sorted({
                manifest_id
                for entity_id in entity_ids
                for manifest_id in manifest_ids_by_member.get(entity_id, ())
            }))
            for evidence_id in evidence_ids:
                span = evidence[evidence_id]
                unit = units.get(span.source_unit_id)
                if unit is None:
                    raise L5bPublicationError(
                        "L5B_SOURCE_UNIT_MISSING",
                        f"evidence {evidence_id} references missing SourceUnit",
                    )
                try:
                    span.verify_against(unit)
                except ValueError as exc:
                    raise L5bPublicationError(
                        "L5B_EVIDENCE_QUOTE_MISMATCH",
                        str(exc),
                    ) from exc
                if span.purpose != "extraction_assertion":
                    raise L5bPublicationError(
                        "L5B_EVIDENCE_PURPOSE_INVALID",
                        f"assertion evidence {evidence_id} has purpose {span.purpose!r}",
                    )
                if span.source_file_id not in source_file_names:
                    raise L5bPublicationError(
                        "L5B_SOURCE_FILE_NAME_MISSING",
                        f"source file name missing for {span.source_file_id}",
                    )
                display_name = _safe_source_display_name(
                    source_file_names[span.source_file_id],
                    source_file_id=span.source_file_id,
                )
                section_path = _safe_section_path(
                    span.locator.section_path or (),
                    source_unit_id=span.source_unit_id,
                )
                asset = asset_by_file.get(span.source_file_id)
                if asset is None:
                    raise L5bPublicationError(
                        "L5B_GOVERNED_SOURCE_ASSET_MISSING",
                        f"evidence {evidence_id} has no exact governed L5a source asset",
                    )
                asset_locator = asset.immutable_locator.to_authority()
                evidence_locator = span.locator.to_authority()
                for field_name in ("char_start", "char_end"):
                    asset_locator.pop(field_name, None)
                    evidence_locator.pop(field_name, None)
                if (
                    asset.asset_version_id != span.asset_version_id
                    or asset.asset_id != span.identity.asset_id
                    or asset.content_hash != span.identity.content_hash
                    or asset.source_file_id != span.source_file_id
                    or asset_locator != evidence_locator
                ):
                    raise L5bPublicationError(
                        "L5B_GOVERNED_ASSET_MISMATCH",
                        f"governed asset differs from evidence {evidence_id}",
                    )
                seed = {
                    "assertion_id": assertion_id,
                    "evidence_span_id": evidence_id,
                    "l4_projection_hash": source.projection.projection_hash,
                }
                document_id = _safe_document_id(seed)
                locator_json = canonical_json(span.locator)
                values: dict[str, Any] = {
                    "id": document_id,
                    "assertion_kind": assertion_kind,
                    "canonical_entity_ids": list(entity_ids),
                    "canonical_relationship_ids": list(relationship_ids),
                    "canonical_property_ids": list(property_ids),
                    "canonical_type_ids": list(type_ids),
                    "canonical_assertion_ids": [assertion_id],
                    "required_member_manifest_ids": list(member_manifest_ids),
                    "source_id": span.source_file_id,
                    "original_document_name": display_name,
                    "source_file_id": span.source_file_id,
                    "asset_id": span.identity.asset_id,
                    "asset_version_id": span.asset_version_id,
                    "asset_hash": span.identity.content_hash,
                    "source_unit_id": span.source_unit_id,
                    "source_unit_hash": canonical_sha256(unit),
                    "source_unit_kind": unit.unit_kind,
                    "source_text_content_hash": unit.text_content_hash,
                    "evidence_span_ids": [evidence_id],
                    "evidence_span_hashes": [canonical_sha256(span)],
                    "evidence_purposes": [span.purpose],
                    "content": span.quote,
                    "source_quote": span.quote,
                    "source_quote_is_verbatim": True,
                    "quote_hash": span.quote_hash,
                    "immutable_locator_json": locator_json,
                    "immutable_locator_hash": span.locator.locator_hash,
                    "page": span.locator.page,
                    "section_path": list(section_path),
                    "access_policy_id": policy.access_policy_id,
                    "access_policy_hash": policy.policy_hash,
                    "acl_principal_keys": list(principal_keys),
                    "acl_scope_keys": list(scope_keys),
                    "authorization_resource_id": policy.authorization_resource_id,
                    "l3_artifact_manifest_id": source.input_manifest.artifact_manifest_id,
                    "l3_artifact_manifest_hash": source.input_manifest.manifest_hash,
                    "l4_projection_hash": source.projection.projection_hash,
                    "l4_receipt_hash": source.receipt.receipt_hash,
                    "l5a_publication_fingerprint": l5a.compiled.fingerprint,
                    "l5a_receipt_hash": l5a.receipt.receipt_hash,
                    "publication_crosswalk_hashes": list(crosswalk_hashes),
                    "asserted_publication_hash": l5a.output_manifest.manifest_hash,
                    "lifecycle_state": "asserted",
                    "governed_asset_reference_id": (
                        asset.governed_asset_reference_id if asset else None
                    ),
                    "governed_asset_reference_hash": (
                        asset.asset_reference_hash if asset else None
                    ),
                    "vector": None,
                    "vector_state": "unavailable",
                }
                values["document_hash"] = canonical_sha256(values)
                if set(values) != set(_SOURCE_DATA_FIELDS):
                    raise L5bPublicationError(
                        "L5B_DOCUMENT_SCHEMA_MISMATCH",
                        "compiled evidence document fields differ from Search select schema",
                    )
                documents.append(values)
    ordered = tuple(sorted(documents, key=lambda item: str(item["id"])))
    ids = [str(item["id"]) for item in ordered]
    if len(ids) != len(set(ids)):
        raise L5bPublicationError(
            "L5B_DOCUMENT_ID_COLLISION",
            "canonical Search document IDs collided",
        )
    return ordered


def _search_field(
    name: str,
    field_type: str,
    *,
    key: bool = False,
    searchable: bool = False,
    filterable: bool = False,
    retrievable: bool = True,
) -> Mapping[str, Any]:
    return {
        "name": name,
        "type": field_type,
        "key": key,
        "searchable": searchable,
        "filterable": filterable,
        "retrievable": retrievable,
    }


def _index_definition(index_name: str) -> Mapping[str, Any]:
    exact_collections = (
        "canonical_entity_ids",
        "canonical_relationship_ids",
        "canonical_property_ids",
        "canonical_type_ids",
        "canonical_assertion_ids",
        "required_member_manifest_ids",
        "evidence_span_ids",
        "evidence_span_hashes",
        "evidence_purposes",
        "acl_principal_keys",
        "acl_scope_keys",
        "publication_crosswalk_hashes",
        "section_path",
    )
    exact_strings = (
        "document_hash",
        "assertion_kind",
        "source_id",
        "source_file_id",
        "asset_id",
        "asset_version_id",
        "asset_hash",
        "source_unit_id",
        "source_unit_hash",
        "source_unit_kind",
        "source_text_content_hash",
        "quote_hash",
        "immutable_locator_hash",
        "access_policy_id",
        "access_policy_hash",
        "authorization_resource_id",
        "l3_artifact_manifest_id",
        "l3_artifact_manifest_hash",
        "l4_projection_hash",
        "l4_receipt_hash",
        "l5a_publication_fingerprint",
        "l5a_receipt_hash",
        "asserted_publication_hash",
        "lifecycle_state",
        "governed_asset_reference_id",
        "governed_asset_reference_hash",
        "vector_state",
    )
    fields = [
        _search_field("id", "Edm.String", key=True, filterable=True),
        *[
            _search_field(
                name,
                "Collection(Edm.String)",
                filterable=True,
                searchable=name in {
                    "canonical_entity_ids",
                    "canonical_assertion_ids",
                },
            )
            for name in exact_collections
        ],
        *[
            _search_field(name, "Edm.String", filterable=True)
            for name in exact_strings
        ],
        _search_field("original_document_name", "Edm.String", searchable=True),
        _search_field("content", "Edm.String", searchable=True),
        _search_field("source_quote", "Edm.String", searchable=True),
        _search_field("source_quote_is_verbatim", "Edm.Boolean", filterable=True),
        _search_field("immutable_locator_json", "Edm.String"),
        _search_field("page", "Edm.Int32", filterable=True),
        {
            **_search_field("vector", "Collection(Edm.Single)", searchable=True),
            "dimensions": 1536,
            "vectorSearchProfile": "evidence-hnsw",
        },
    ]
    return {
        "name": index_name,
        "fields": fields,
        "semantic": {
            "configurations": [{
                "name": "evidence-semantic",
                "prioritizedFields": {
                    "titleField": {"fieldName": "original_document_name"},
                    "prioritizedContentFields": [
                        {"fieldName": "content"},
                        {"fieldName": "source_quote"},
                    ],
                    "prioritizedKeywordsFields": [
                        {"fieldName": "canonical_entity_ids"},
                        {"fieldName": "canonical_assertion_ids"},
                    ],
                },
            }]
        },
        "vectorSearch": {
            "algorithms": [{
                "name": "evidence-hnsw-algorithm",
                "kind": "hnsw",
                "hnswParameters": {
                    "m": 4,
                    "efConstruction": 400,
                    "efSearch": 500,
                    "metric": "cosine",
                },
            }],
            "profiles": [{
                "name": "evidence-hnsw",
                "algorithm": "evidence-hnsw-algorithm",
            }],
        },
    }


def _odata_literal(value: str) -> str:
    if not value or any(ord(char) < 32 for char in value):
        raise L5bPublicationError("L5B_FILTER_UNSAFE", "canonical filter value is invalid")
    return "'" + value.replace("'", "''") + "'"


def canonical_scope_filter(
    *,
    canonical_entity_ids: Sequence[str],
    canonical_type_ids: Sequence[str],
    canonical_relationship_ids: Sequence[str],
    access_policy_hash: str,
    asserted_publication_hash: str,
) -> str:
    """Compile exact canonical OData clauses without display-name parsing."""

    def any_clause(field: str, values: Sequence[str], variable: str) -> str:
        ordered = tuple(sorted(set(values)))
        if len(ordered) != len(tuple(values)):
            raise L5bPublicationError(
                "L5B_SCOPE_KEY_COLLISION",
                f"{field} contains duplicate canonical IDs",
            )
        if not ordered:
            return ""
        inner = " or ".join(
            f"{variable} eq {_odata_literal(value)}" for value in ordered
        )
        return f"{field}/any({variable}: {inner})"

    clauses = [
        f"access_policy_hash eq {_odata_literal(access_policy_hash)}",
        f"asserted_publication_hash eq {_odata_literal(asserted_publication_hash)}",
        "source_quote_is_verbatim eq true",
        "lifecycle_state eq 'asserted'",
        any_clause("canonical_entity_ids", canonical_entity_ids, "entity"),
        any_clause("canonical_type_ids", canonical_type_ids, "type"),
        any_clause(
            "canonical_relationship_ids",
            canonical_relationship_ids,
            "relationship",
        ),
    ]
    return " and ".join(item for item in clauses if item)


def compile_l5b_publication(
    source: SealedL4ServingSource,
    l5a_result: L5aStageResult,
    *,
    evidence_partitions: Mapping[str, Sequence[EvidenceSpanV1_1]],
    source_unit_manifest: ArtifactManifest,
    source_units: Sequence[SourceUnit],
    source_file_names: Mapping[str, str],
    access_policy: AccessPolicy,
    governed_assets: Sequence[GovernedAssetReference],
    target_id: str,
    index_name: str,
    knowledge_source_name: str,
    knowledge_base_name: str,
) -> L5bCompiledPublication:
    """Compile deterministic Search artifacts from sealed L5a/L4/L3 authority."""

    if not isinstance(source, SealedL4ServingSource):
        raise L5bPublicationError(
            "L5B_SCHEMA2_SOURCE_REQUIRED",
            "L5b accepts only SealedL4ServingSource",
        )
    if not isinstance(l5a_result, L5aStageResult):
        raise L5bPublicationError(
            "L5B_L5A_RESULT_REQUIRED",
            "L5b accepts only a successful sealed L5a stage result",
        )
    require_l5a_publication_receipt(source, l5a_result)
    if l5a_result.receipt.status not in {"succeeded", "skipped"}:
        raise L5bPublicationError(
            "L5B_L5A_PUBLICATION_UNASSERTED",
            "L5a publication must be successful",
        )
    if (
        access_policy != l5a_result.compiled.access_policy
        or access_policy.identity.project_id != source.receipt.identity.project_id
        or access_policy.identity.semantic_contract_hash
        != source.projection.sealed_semantic_contract_hash
    ):
        raise L5bPublicationError(
            "L5B_ACCESS_POLICY_STALE",
            "L5b access policy differs from sealed L5a authority",
        )
    if any(not item.strip() for item in (
        target_id,
        index_name,
        knowledge_source_name,
        knowledge_base_name,
    )):
        raise L5bPublicationError(
            "L5B_TARGET_ID_INVALID",
            "Search target and resource names must be non-empty",
        )
    evidence = _validate_evidence_partitions(source, evidence_partitions)
    ordered_assets = _validate_governed_asset_input(
        l5a_result,
        governed_assets,
        access_policy,
    )
    source_file_ids = {item.source_file_id for item in source_units}
    if set(source_file_names) != source_file_ids:
        raise L5bPublicationError(
            "L5B_SOURCE_FILE_NAME_SET_MISMATCH",
            "source display names must exactly cover sealed SourceUnit source files",
        )
    safe_source_file_names = {
        source_file_id: _safe_source_display_name(
            source_file_names[source_file_id],
            source_file_id=source_file_id,
        )
        for source_file_id in sorted(source_file_ids)
    }
    documents = _assertion_documents(
        source,
        l5a_result,
        evidence=evidence,
        source_unit_manifest=source_unit_manifest,
        source_units=source_units,
        source_file_names=safe_source_file_names,
        policy=access_policy,
        assets=ordered_assets,
    )
    index_definition = _index_definition(index_name)
    base_filter = canonical_scope_filter(
        canonical_entity_ids=(),
        canonical_type_ids=(),
        canonical_relationship_ids=(),
        access_policy_hash=access_policy.policy_hash,
        asserted_publication_hash=l5a_result.output_manifest.manifest_hash,
    )
    knowledge_source_definition = {
        "name": knowledge_source_name,
        "kind": "searchIndex",
        "description": "Sealed fabric-kg evidence index; zero synthesis.",
        "encryptionKey": None,
        "searchIndexParameters": {
            "searchIndexName": index_name,
            "semanticConfigurationName": "evidence-semantic",
            "sourceDataFields": [{"name": item} for item in _SOURCE_DATA_FIELDS],
            "searchFields": [
                {"name": "content"},
                {"name": "source_quote"},
            ],
            "baseFilter": base_filter,
        },
    }
    knowledge_base_definition = {
        "name": knowledge_base_name,
        "description": "Extractive evidence delivery only; no answer synthesis.",
        "knowledgeSources": [{"name": knowledge_source_name}],
        "outputMode": "extractiveData",
        "retrievalReasoningEffort": {"kind": "minimal"},
        "models": [],
    }
    document_ids = tuple(str(item["id"]) for item in documents)
    document_hashes = tuple(
        (str(item["id"]), str(item["document_hash"])) for item in documents
    )
    vector_state_hash = canonical_sha256(
        [(item["id"], item["vector_state"], item["vector"]) for item in documents]
    )
    index_fingerprint = canonical_sha256({
        "index_definition": index_definition,
        "knowledge_source_definition": knowledge_source_definition,
        "knowledge_base_definition": knowledge_base_definition,
        "document_hashes": document_hashes,
        "vector_state_hash": vector_state_hash,
    })
    fingerprint = canonical_sha256({
        "stage": L5B_STAGE_NAME,
        "stage_contract_version": L5B_STAGE_CONTRACT_VERSION,
        "publication_code_version": L5B_PUBLICATION_CODE_VERSION,
        "l3_manifest_hash": source.input_manifest.manifest_hash,
        "source_unit_manifest_hash": source_unit_manifest.manifest_hash,
        "l4_receipt_hash": source.receipt.receipt_hash,
        "l4_projection_hash": source.projection.projection_hash,
        "l5a_fingerprint": l5a_result.compiled.fingerprint,
        "l5a_receipt_hash": l5a_result.receipt.receipt_hash,
        "access_policy_hash": access_policy.policy_hash,
        "governed_asset_hashes": sorted(
            item.asset_reference_hash for item in ordered_assets
        ),
        "target_id": target_id,
        "index_fingerprint": index_fingerprint,
    })
    return L5bCompiledPublication(
        source=source,
        l5a_result=l5a_result,
        fingerprint=fingerprint,
        target_id=target_id,
        index_name=index_name,
        knowledge_source_name=knowledge_source_name,
        knowledge_base_name=knowledge_base_name,
        index_definition=index_definition,
        knowledge_source_definition=knowledge_source_definition,
        knowledge_base_definition=knowledge_base_definition,
        documents=documents,
        document_ids=document_ids,
        document_hashes=document_hashes,
        vector_state_hash=vector_state_hash,
        index_fingerprint=index_fingerprint,
        access_policy=access_policy,
        governed_assets=tuple(sorted(
            ordered_assets,
            key=lambda item: item.governed_asset_reference_id,
        )),
    )


def _expected_state(
    compiled: L5bCompiledPublication,
    publication_token: str,
) -> L5bTargetState:
    return L5bTargetState(
        target_id=compiled.target_id,
        target_version=L5B_TARGET_VERSION,
        index_definition=compiled.index_definition,
        knowledge_source_definition=compiled.knowledge_source_definition,
        knowledge_base_definition=compiled.knowledge_base_definition,
        document_ids=compiled.document_ids,
        document_hashes=compiled.document_hashes,
        vector_state_hash=compiled.vector_state_hash,
        access_policy_id=compiled.access_policy.access_policy_id,
        access_policy_hash=compiled.access_policy.policy_hash,
        publication_token=publication_token,
    )


def _validate_state(
    actual: L5bTargetState | None,
    expected: L5bTargetState,
    *,
    phase: str,
) -> None:
    if actual is None:
        raise L5bPublicationError(
            "L5B_TARGET_MISSING",
            f"Search target is missing during {phase}",
        )
    checks = (
        ("target identity", actual.target_id, expected.target_id),
        ("target version", actual.target_version, expected.target_version),
        (
            "index definition",
            canonical_sha256(actual.index_definition),
            canonical_sha256(expected.index_definition),
        ),
        (
            "knowledge source",
            canonical_sha256(actual.knowledge_source_definition),
            canonical_sha256(expected.knowledge_source_definition),
        ),
        (
            "knowledge base",
            canonical_sha256(actual.knowledge_base_definition),
            canonical_sha256(expected.knowledge_base_definition),
        ),
        ("document IDs", actual.document_ids, expected.document_ids),
        ("document hashes", actual.document_hashes, expected.document_hashes),
        ("vector state", actual.vector_state_hash, expected.vector_state_hash),
        ("access policy ID", actual.access_policy_id, expected.access_policy_id),
        ("access policy hash", actual.access_policy_hash, expected.access_policy_hash),
        ("publication token", actual.publication_token, expected.publication_token),
    )
    for name, observed, wanted in checks:
        if observed != wanted:
            raise L5bPublicationError(
                "L5B_READ_BACK_MISMATCH",
                f"{name} differs during {phase}",
            )


def _write_json(path: Path, value: Any) -> bytes:
    payload = (canonical_json(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _persist_compiled(
    compiled: L5bCompiledPublication,
    root: Path,
) -> tuple[Path, Path, Path, Path]:
    paths = (
        root / "index-definition.json",
        root / "knowledge-source-definition.json",
        root / "knowledge-base-definition.json",
        root / "documents.json",
    )
    for path, value in zip(
        paths,
        (
            compiled.index_definition,
            compiled.knowledge_source_definition,
            compiled.knowledge_base_definition,
            compiled.documents,
        ),
    ):
        _write_json(path, value)
        if json.loads(path.read_text("utf-8")) != json.loads(canonical_json(value)):
            raise L5bPublicationError(
                "L5B_LOCAL_MATERIALIZATION_DRIFT",
                f"persisted artifact differs for {path.name}",
            )
    return paths


def _projection_equivalences(
    compiled: L5bCompiledPublication,
) -> tuple[ProjectionEquivalenceV1_1, ...]:
    evidence = ProjectionEvidence(
        count=len(compiled.documents),
        canonical_id_set_hash=canonical_sha256(compiled.document_ids),
        index_fingerprint=compiled.index_fingerprint,
    )
    proofs = []
    for crosswalk in compiled.l5a_result.compiled.crosswalks:
        values = {
            "identity": ProjectionEquivalenceIdentityV1_1.model_validate({
                **_identity(
                    compiled.source,
                    "c0.projection_equivalence",
                ).model_dump(mode="python"),
                "contract_version": "1.1.0",
            }),
            "projection_equivalence_id": deterministic_contract_id(
                "projection-equivalence",
                {
                    "stage": "L5b",
                    "crosswalk": crosswalk.crosswalk_hash,
                    "index": compiled.index_fingerprint,
                },
            ),
            "authority": crosswalk.authority,
            "publication_crosswalk_id": crosswalk.publication_crosswalk_id,
            "publication_crosswalk_hash": crosswalk.crosswalk_hash,
            "source_projection_id": crosswalk.source_projection_id,
            "source_projection_hash": crosswalk.source_projection_hash,
            "projection_kind": "search",
            "expected": evidence,
            "compiled": evidence,
            "deployed": evidence,
            "read_back": evidence,
            "missing_canonical_ids": (),
            "extra_canonical_ids": (),
            "equivalent": True,
        }
        proofs.append(ProjectionEquivalenceV1_1(
            **values,
            equivalence_hash=canonical_sha256(values),
        ))
    return tuple(proofs)


def _artifact_manifest(
    compiled: L5bCompiledPublication,
    proofs: Sequence[ProjectionEquivalenceV1_1],
) -> ArtifactManifest:
    files = (
        ("index-definition", "l5b.search_index_definition", compiled.index_definition),
        (
            "knowledge-source",
            "l5b.knowledge_source_definition",
            compiled.knowledge_source_definition,
        ),
        (
            "knowledge-base",
            "l5b.knowledge_base_definition",
            compiled.knowledge_base_definition,
        ),
        ("documents", "l5b.evidence_documents", compiled.documents),
        ("projection-equivalence", "c0.projection_equivalence", tuple(proofs)),
    )
    entries = []
    for label, kind, value in files:
        payload = (canonical_json(value) + "\n").encode("utf-8")
        row_count = (
            len(compiled.documents)
            if label == "documents"
            else len(proofs)
            if label == "projection-equivalence"
            else 1
        )
        entries.append(ArtifactEntry(
            artifact_id=deterministic_contract_id(
                "l5b-artifact",
                {"fingerprint": compiled.fingerprint, "label": label},
            ),
            contract_kind=kind,
            contract_version="1.0.0",
            schema_hash=canonical_sha256({"kind": kind, "version": "1.0.0"}),
            content_hash=canonical_sha256(value),
            canonical_id_set_hash=(
                canonical_sha256(compiled.document_ids)
                if label == "documents"
                else None
            ),
            row_count=row_count,
            byte_count=len(payload),
            partition_count=1,
            media_type="application/json",
            immutable_locator=None,
            blob_asset_ref_id=None,
        ))
    entries.sort(key=lambda item: item.artifact_id)
    values = {
        "identity": _identity(compiled.source, "c0.artifact_manifest"),
        "artifact_manifest_id": deterministic_contract_id(
            "artifact-manifest",
            {"stage": "L5b", "fingerprint": compiled.fingerprint},
        ),
        "entries": tuple(entries),
        "total_row_count": sum(item.row_count or 0 for item in entries),
        "total_byte_count": sum(item.byte_count for item in entries),
    }
    return ArtifactManifest(**values, manifest_hash=canonical_sha256(values))


def _budget_snapshot_hash() -> str:
    return canonical_sha256({
        "stage": "L5b",
        "success_search_calls": L5B_MAX_SUCCESS_SEARCH_CALLS,
        "worst_case_search_calls": L5B_MAX_SEARCH_CALLS,
        "state_machine": {
            "reuse_read_back": L5B_REUSE_READ_BACK_CALLS,
            "inspect": L5B_INSPECT_CALLS,
            "publish": L5B_PUBLISH_CALLS,
            "post_publish_read_back": L5B_POST_PUBLISH_READ_BACK_CALLS,
            "rollback_mutation": L5B_ROLLBACK_MUTATION_CALLS,
            "ambiguous_recovery_inspect": L5B_AMBIGUOUS_RECOVERY_INSPECT_CALLS,
        },
    })


def _new_publication_token(compiled: L5bCompiledPublication) -> str:
    nonce = uuid.uuid4().hex
    seal = canonical_sha256({
        "stage": "L5b",
        "fingerprint": compiled.fingerprint,
        "nonce": nonce,
    })
    return f"{nonce}.{seal}"


def _publication_token_is_valid(
    compiled: L5bCompiledPublication,
    token: str,
) -> bool:
    parts = token.split(".")
    if (
        len(parts) != 2
        or re.fullmatch(r"[0-9a-f]{32}", parts[0]) is None
        or re.fullmatch(r"[0-9a-f]{64}", parts[1]) is None
    ):
        return False
    return parts[1] == canonical_sha256({
        "stage": "L5b",
        "fingerprint": compiled.fingerprint,
        "nonce": parts[0],
    })


def _metrics(
    compiled: L5bCompiledPublication,
    *,
    started: float,
    cpu_started: float,
    rss_started: int,
    accounting: _PublicationAccounting,
    storage_write_bytes: int,
    cache_hits: int,
) -> StageResourceMetrics:
    usage = process_resource_usage()
    values = {
        "identity": _identity(compiled.source, "c0.stage_resource_metrics"),
        "resource_metrics_id": deterministic_contract_id(
            "stage-resource-metrics",
            {
                "stage": "L5b",
                "fingerprint": compiled.fingerprint,
                "search_calls": accounting.search_calls,
                "cache_hits": cache_hits,
            },
        ),
        "stage_id": "L5",
        "stage_name": L5B_STAGE_NAME,
        "wall_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "cpu_ms": max(0, int((time.process_time() - cpu_started) * 1000)),
        "peak_rss_bytes": max(0, usage.peak_rss_bytes - rss_started),
        "storage_read_bytes": (
            compiled.source.manifest.total_byte_count
            + compiled.l5a_result.output_manifest.total_byte_count
        ),
        "storage_write_bytes": storage_write_bytes,
        "network_request_bytes": accounting.request_bytes,
        "network_response_bytes": accounting.response_bytes,
        "source_units_read": len({
            item["source_unit_id"] for item in compiled.documents
        }),
        "source_units_written": 0,
        "source_units_skipped": 0,
        "document_intelligence_calls": 0,
        "document_intelligence_pages": 0,
        "foundry_calls": 0,
        "foundry_input_tokens": 0,
        "foundry_output_tokens": 0,
        "embedding_calls": 0,
        "embedding_items": 0,
        "fabric_calls": 0,
        "fabric_rows_read": 0,
        "fabric_rows_written": 0,
        "search_calls": accounting.search_calls,
        "search_documents_read": accounting.search_documents_read,
        "search_documents_written": accounting.search_documents_written,
        "retry_count": accounting.retry_count,
        "retry_wait_ms": accounting.retry_wait_ms,
        "cache_hits": cache_hits,
        "cache_misses": 0 if cache_hits else 1,
        "max_observed_concurrency": 1 if accounting.search_calls else 0,
        "budget_snapshot_hash": _budget_snapshot_hash(),
        "exceeded_dimensions": (),
    }
    return StageResourceMetrics(**values, metrics_hash=canonical_sha256(values))


def _receipt(
    compiled: L5bCompiledPublication,
    *,
    status: Literal["succeeded", "skipped", "failed"],
    manifest: ArtifactManifest | None,
    metrics: StageResourceMetrics,
    accounting: _PublicationAccounting,
    started_at: datetime,
    error_codes: Sequence[str] = (),
) -> StageReceipt:
    values = {
        "identity": _identity(compiled.source, "c0.stage_receipt"),
        "stage_receipt_id": deterministic_contract_id(
            "stage-receipt",
            {
                "stage": "L5b",
                "fingerprint": compiled.fingerprint,
                "status": status,
                "search_calls": accounting.search_calls,
            },
        ),
        "stage_id": "L5",
        "stage_name": L5B_STAGE_NAME,
        "stage_contract_version": L5B_STAGE_CONTRACT_VERSION,
        "status": status,
        "input_manifest_id": compiled.l5a_result.output_manifest.artifact_manifest_id,
        "input_manifest_hash": compiled.l5a_result.output_manifest.manifest_hash,
        "output_manifest_id": manifest.artifact_manifest_id if manifest else None,
        "output_manifest_hash": manifest.manifest_hash if manifest else None,
        "skip_key": compiled.fingerprint,
        "accepted_contract_versions": L5B_ACCEPTED_VERSIONS,
        "resource_metrics_id": metrics.resource_metrics_id,
        "resource_metrics_hash": metrics.metrics_hash,
        "attempt_count": 1,
        "remote_operation_refs": accounting.remote_operation_refs,
        "error_codes": tuple(sorted(set(error_codes))),
        "started_at_utc": started_at,
        "completed_at_utc": datetime.now(timezone.utc),
    }
    return StageReceipt(**values, receipt_hash=canonical_sha256({
        key: value
        for key, value in values.items()
        if key not in {"started_at_utc", "completed_at_utc"}
    }))


def _checkpoint_path(run_root: Path) -> Path:
    return run_root.parent.parent / "checkpoints" / f"{run_root.name}.json"


def _checkpoint_payload(
    compiled: L5bCompiledPublication,
    manifest: ArtifactManifest,
    metrics: StageResourceMetrics,
    receipt: StageReceipt,
    *,
    signer: _ResolvedCheckpointSigner,
) -> Mapping[str, Any]:
    tokens = tuple(
        item.removeprefix("publication-token:")
        for item in receipt.remote_operation_refs
        if item.startswith("publication-token:")
    )
    if len(tokens) != 1 or not _publication_token_is_valid(compiled, tokens[0]):
        raise L5bPublicationError(
            "L5B_PUBLICATION_TOKEN_INVALID",
            "checkpoint requires one fingerprint-bound publication token",
        )
    values = {
        "stage": "L5b",
        "checkpoint_key_id": signer.identity.key_id,
        "checkpoint_key_version": signer.identity.key_version,
        "checkpoint_algorithm": signer.identity.algorithm,
        "fingerprint": compiled.fingerprint,
        "index_fingerprint": compiled.index_fingerprint,
        "l3_artifact_manifest_hash": compiled.source.input_manifest.manifest_hash,
        "l4_projection_hash": compiled.source.projection.projection_hash,
        "l4_receipt_hash": compiled.source.receipt.receipt_hash,
        "l5a_publication_fingerprint": compiled.l5a_result.compiled.fingerprint,
        "l5a_receipt_hash": compiled.l5a_result.receipt.receipt_hash,
        "artifact_manifest_hash": manifest.manifest_hash,
        "metrics_hash": metrics.metrics_hash,
        "receipt_hash": receipt.receipt_hash,
        "metrics_payload_hash": canonical_sha256(metrics.model_dump(mode="json")),
        "receipt_payload_hash": canonical_sha256(receipt.model_dump(mode="json")),
        "publication_token": tokens[0],
        "compiled_payload_hashes": {
            "index_definition": canonical_sha256(compiled.index_definition),
            "knowledge_source_definition": canonical_sha256(
                compiled.knowledge_source_definition
            ),
            "knowledge_base_definition": canonical_sha256(
                compiled.knowledge_base_definition
            ),
            "documents": canonical_sha256(compiled.documents),
            "projection_equivalence": canonical_sha256(
                _projection_equivalences(compiled)
            ),
        },
    }
    sealed = {**values, "checkpoint_hash": canonical_sha256(values)}
    return sealed


def _checkpoint(
    compiled: L5bCompiledPublication,
    manifest: ArtifactManifest,
    metrics: StageResourceMetrics,
    receipt: StageReceipt,
    *,
    signer: _ResolvedCheckpointSigner,
) -> Mapping[str, Any]:
    payload = _checkpoint_payload(
        compiled,
        manifest,
        metrics,
        receipt,
        signer=signer,
    )
    return {**payload, "checkpoint_mac": _sign_checkpoint_payload(signer, payload)}


def _checkpoint_unsigned(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key != "checkpoint_mac"
    }


def _checkpoint_is_authentic(
    persisted: Mapping[str, Any],
    expected: Mapping[str, Any],
    signer: _ResolvedCheckpointSigner,
) -> bool:
    if not isinstance(persisted, Mapping):
        return False
    persisted_unsigned = _checkpoint_unsigned(persisted)
    expected_unsigned = _checkpoint_unsigned(expected)
    if persisted_unsigned != expected_unsigned:
        return False
    return _verify_checkpoint_payload(
        signer,
        expected_unsigned,
        persisted.get("checkpoint_mac"),
    )


def _persist_checkpoint(
    run_root: Path,
    compiled: L5bCompiledPublication,
    manifest: ArtifactManifest,
    metrics: StageResourceMetrics,
    receipt: StageReceipt,
    signer: _ResolvedCheckpointSigner | None,
) -> None:
    if signer is None:
        return
    path = _checkpoint_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _write_json(
        temp_path,
        _checkpoint(
            compiled,
            manifest,
            metrics,
            receipt,
            signer=signer,
        ),
    )
    os.replace(temp_path, path)


def _skip_checkpoint_path(run_root: Path, receipt: StageReceipt) -> Path:
    return (
        run_root.parent.parent
        / "checkpoints"
        / "skips"
        / f"{receipt.receipt_hash}.json"
    )


def _skip_checkpoint_payload(
    run_root: Path,
    compiled: L5bCompiledPublication,
    metrics: StageResourceMetrics,
    receipt: StageReceipt,
    *,
    signer: _ResolvedCheckpointSigner,
) -> Mapping[str, Any]:
    succeeded_checkpoint = json.loads(
        _checkpoint_path(run_root).read_text("utf-8")
    )
    values = {
        "stage": "L5b-skip",
        "checkpoint_key_id": signer.identity.key_id,
        "checkpoint_key_version": signer.identity.key_version,
        "checkpoint_algorithm": signer.identity.algorithm,
        "fingerprint": compiled.fingerprint,
        "succeeded_checkpoint_hash": canonical_sha256(succeeded_checkpoint),
        "metrics_hash": metrics.metrics_hash,
        "metrics_payload_hash": canonical_sha256(metrics.model_dump(mode="json")),
        "receipt_hash": receipt.receipt_hash,
        "receipt_payload_hash": canonical_sha256(receipt.model_dump(mode="json")),
    }
    sealed = {**values, "checkpoint_hash": canonical_sha256(values)}
    return sealed


def _skip_checkpoint(
    run_root: Path,
    compiled: L5bCompiledPublication,
    metrics: StageResourceMetrics,
    receipt: StageReceipt,
    *,
    signer: _ResolvedCheckpointSigner,
) -> Mapping[str, Any]:
    payload = _skip_checkpoint_payload(
        run_root,
        compiled,
        metrics,
        receipt,
        signer=signer,
    )
    return {**payload, "checkpoint_mac": _sign_checkpoint_payload(signer, payload)}


def _persist_skip_checkpoint(
    run_root: Path,
    compiled: L5bCompiledPublication,
    metrics: StageResourceMetrics,
    receipt: StageReceipt,
    signer: _ResolvedCheckpointSigner,
) -> None:
    path = _skip_checkpoint_path(run_root, receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _write_json(
        temp_path,
        _skip_checkpoint(
            run_root,
            compiled,
            metrics,
            receipt,
            signer=signer,
        ),
    )
    os.replace(temp_path, path)


def _skip_checkpoint_is_valid(
    run_root: Path,
    compiled: L5bCompiledPublication,
    metrics: StageResourceMetrics,
    receipt: StageReceipt,
    signer: _ResolvedCheckpointSigner,
) -> bool:
    try:
        persisted = json.loads(
            _skip_checkpoint_path(run_root, receipt).read_text("utf-8")
        )
        expected = _skip_checkpoint_payload(
            run_root,
            compiled,
            metrics,
            receipt,
            signer=signer,
        )
    except (L5bPublicationError, OSError, ValueError, json.JSONDecodeError):
        return False
    return _checkpoint_is_authentic(persisted, expected, signer)


def _load_intact(
    compiled: L5bCompiledPublication,
    run_root: Path,
    signer: _ResolvedCheckpointSigner | None,
) -> tuple[
    ArtifactManifest,
    tuple[ProjectionEquivalenceV1_1, ...],
    StageReceipt,
    StageResourceMetrics,
] | None:
    try:
        expected_proofs = _projection_equivalences(compiled)
        expected_payloads = {
            "index-definition.json": compiled.index_definition,
            "knowledge-source-definition.json": (
                compiled.knowledge_source_definition
            ),
            "knowledge-base-definition.json": compiled.knowledge_base_definition,
            "documents.json": compiled.documents,
            "projection-equivalence.json": expected_proofs,
        }
        for filename, expected_value in expected_payloads.items():
            if (run_root / filename).read_bytes() != (
                canonical_json(expected_value) + "\n"
            ).encode("utf-8"):
                return None
        manifest = ArtifactManifest.model_validate_json(
            (run_root / "output-manifest.json").read_text("utf-8")
        )
        metrics = StageResourceMetrics.model_validate_json(
            (run_root / "resource-metrics.json").read_text("utf-8")
        )
        receipt = StageReceipt.model_validate_json(
            (run_root / "stage-receipt.json").read_text("utf-8")
        )
        checkpoint = (
            json.loads(_checkpoint_path(run_root).read_text("utf-8"))
            if signer is not None
            else None
        )
        proofs = tuple(
            ProjectionEquivalenceV1_1.model_validate(item)
            for item in json.loads(
                (run_root / "projection-equivalence.json").read_text("utf-8")
            )
        )
        expected = _artifact_manifest(compiled, expected_proofs)
        validate_receipt_resources(receipt, metrics)
        remote_refs = tuple(
            item
            for item in receipt.remote_operation_refs
            if not item.startswith("publication-token:")
        )
        publication_tokens = tuple(
            item.removeprefix("publication-token:")
            for item in receipt.remote_operation_refs
            if item.startswith("publication-token:")
        )
        expected_metrics_id = deterministic_contract_id(
            "stage-resource-metrics",
            {
                "stage": "L5b",
                "fingerprint": compiled.fingerprint,
                "search_calls": metrics.search_calls,
                "cache_hits": 0,
            },
        )
        expected_receipt_id = deterministic_contract_id(
            "stage-receipt",
            {
                "stage": "L5b",
                "fingerprint": compiled.fingerprint,
                "status": "succeeded",
                "search_calls": metrics.search_calls,
            },
        )
        expected_storage_write_bytes = sum(
            len((canonical_json(value) + "\n").encode("utf-8"))
            for value in expected_payloads.values()
        ) + len((canonical_json(expected) + "\n").encode("utf-8"))
        if (
            manifest != expected
            or receipt.status != "succeeded"
            or receipt.stage_receipt_id != expected_receipt_id
            or receipt.identity != _identity(compiled.source, "c0.stage_receipt")
            or receipt.stage_id != "L5"
            or receipt.stage_name != L5B_STAGE_NAME
            or receipt.stage_contract_version != L5B_STAGE_CONTRACT_VERSION
            or receipt.input_manifest_id
            != compiled.l5a_result.output_manifest.artifact_manifest_id
            or receipt.input_manifest_hash
            != compiled.l5a_result.output_manifest.manifest_hash
            or receipt.output_manifest_id != expected.artifact_manifest_id
            or receipt.skip_key != compiled.fingerprint
            or receipt.output_manifest_hash != manifest.manifest_hash
            or dict(receipt.accepted_contract_versions) != L5B_ACCEPTED_VERSIONS
            or receipt.attempt_count != 1
            or receipt.error_codes
            or len(publication_tokens) != 1
            or not _publication_token_is_valid(compiled, publication_tokens[0])
            or len(remote_refs) != metrics.search_calls
            or metrics.resource_metrics_id != expected_metrics_id
            or metrics.identity
            != _identity(compiled.source, "c0.stage_resource_metrics")
            or metrics.stage_id != "L5"
            or metrics.stage_name != L5B_STAGE_NAME
            or metrics.storage_read_bytes
            != (
                compiled.source.manifest.total_byte_count
                + compiled.l5a_result.output_manifest.total_byte_count
            )
            or metrics.storage_write_bytes != expected_storage_write_bytes
            or metrics.source_units_read
            != len({item["source_unit_id"] for item in compiled.documents})
            or metrics.source_units_written
            or metrics.source_units_skipped
            or metrics.document_intelligence_calls
            or metrics.document_intelligence_pages
            or metrics.foundry_calls
            or metrics.foundry_input_tokens
            or metrics.foundry_output_tokens
            or metrics.embedding_calls
            or metrics.embedding_items
            or metrics.fabric_calls
            or metrics.fabric_rows_read
            or metrics.fabric_rows_written
            or metrics.search_calls not in {3, 4}
            or metrics.search_documents_written != len(compiled.documents)
            or metrics.cache_hits != 0
            or metrics.cache_misses != 1
            or metrics.max_observed_concurrency != 1
            or metrics.budget_snapshot_hash != _budget_snapshot_hash()
            or metrics.exceeded_dimensions
            or proofs != expected_proofs
            or (run_root / "output-manifest.json").read_bytes()
            != (canonical_json(expected) + "\n").encode("utf-8")
            or (run_root / "resource-metrics.json").read_bytes()
            != (canonical_json(metrics) + "\n").encode("utf-8")
            or (run_root / "stage-receipt.json").read_bytes()
            != (canonical_json(receipt) + "\n").encode("utf-8")
            or (
                signer is not None
                and not _checkpoint_is_authentic(
                    checkpoint,
                    _checkpoint_payload(
                        compiled,
                        expected,
                        metrics,
                        receipt,
                        signer=signer,
                    ),
                    signer,
                )
            )
        ):
            return None
        return manifest, proofs, receipt, metrics
    except (
        L5bPublicationError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        return None


def run_l5b(
    source: SealedL4ServingSource,
    l5a_result: L5aStageResult,
    *,
    evidence_partitions: Mapping[str, Sequence[EvidenceSpanV1_1]],
    source_unit_manifest: ArtifactManifest,
    source_units: Sequence[SourceUnit],
    source_file_names: Mapping[str, str],
    access_policy: AccessPolicy,
    governed_assets: Sequence[GovernedAssetReference],
    target_id: str,
    index_name: str,
    knowledge_source_name: str,
    knowledge_base_name: str,
    client: L5bTargetClient,
    checkpoint_integrity_signer: CheckpointIntegritySigner | None = None,
    state_root: Path = L5B_STATE_DIR,
) -> L5bStageResult:
    """Publish and read back one atomic Search evidence resource set."""

    started = time.perf_counter()
    cpu_started = time.process_time()
    rss_started = process_resource_usage().peak_rss_bytes
    started_at = datetime.now(timezone.utc)
    compiled = compile_l5b_publication(
        source,
        l5a_result,
        evidence_partitions=evidence_partitions,
        source_unit_manifest=source_unit_manifest,
        source_units=source_units,
        source_file_names=source_file_names,
        access_policy=access_policy,
        governed_assets=governed_assets,
        target_id=target_id,
        index_name=index_name,
        knowledge_source_name=knowledge_source_name,
        knowledge_base_name=knowledge_base_name,
    )
    run_root = state_root / "runs" / compiled.fingerprint
    accounting = _PublicationAccounting()
    checkpoint_signer = _resolve_checkpoint_signer(checkpoint_integrity_signer)
    intact = (
        _load_intact(compiled, run_root, checkpoint_signer)
        if checkpoint_signer is not None
        else None
    )
    if intact is not None:
        token = next(
            item.removeprefix("publication-token:")
            for item in intact[2].remote_operation_refs
            if item.startswith("publication-token:")
        )
        operation = accounting.invoke(
            "reuse-read-back",
            lambda: client.read_back(compiled.target_id),
        )
        if operation.state is not None:
            accounting.search_documents_read += len(operation.state.document_ids)
        try:
            _validate_state(
                operation.state,
                _expected_state(compiled, token),
                phase="reuse",
            )
        except L5bPublicationError as exc:
            if exc.code not in {"L5B_TARGET_MISSING", "L5B_READ_BACK_MISMATCH"}:
                raise
            intact = None
        else:
            accounting.require_complete()
            metrics = _metrics(
                compiled,
                started=started,
                cpu_started=cpu_started,
                rss_started=rss_started,
                accounting=accounting,
                storage_write_bytes=0,
                cache_hits=1,
            )
            receipt = _receipt(
                compiled,
                status="skipped",
                manifest=intact[0],
                metrics=metrics,
                accounting=accounting,
                started_at=started_at,
            )
            _persist_skip_checkpoint(
                run_root,
                compiled,
                metrics,
                receipt,
                checkpoint_signer,
            )
            return L5bStageResult(
                compiled=compiled,
                projection_equivalences=intact[1],
                output_manifest=intact[0],
                metrics=metrics,
                receipt=receipt,
                run_root=run_root,
                reused=True,
            )

    state_root.mkdir(parents=True, exist_ok=True)
    token = _new_publication_token(compiled)
    accounting.remote_operation_refs = tuple(sorted({
        *accounting.remote_operation_refs,
        f"publication-token:{token}",
    }))
    temp_root = Path(tempfile.mkdtemp(
        prefix=f".l5b-{compiled.fingerprint[:12]}-",
        dir=state_root,
    ))
    prior: L5bTargetState | None = None
    publish_started = False
    publish_applied = False
    created = False
    try:
        paths = _persist_compiled(compiled, temp_root)
        prior = accounting.invoke(
            "inspect",
            lambda: client.inspect(compiled.target_id),
        ).state
        if prior is not None:
            accounting.search_documents_read += len(prior.document_ids)
        if prior is not None and prior.target_version != L5B_TARGET_VERSION:
            raise L5bPublicationError(
                "L5B_TARGET_VERSION_UNSUPPORTED",
                f"Search target version {prior.target_version!r} is unsupported",
            )
        publish_started = True
        mutation = accounting.invoke(
            "publish",
            lambda: client.publish(
                compiled.target_id,
                index_definition_path=paths[0],
                knowledge_source_definition_path=paths[1],
                knowledge_base_definition_path=paths[2],
                documents_path=paths[3],
                batch_size=L5B_MAX_BATCH_SIZE,
                expected_state=prior,
                publication_token=token,
            ),
        )
        if (
            mutation.target_id != compiled.target_id
            or mutation.publication_token != token
            or not mutation.applied
            or mutation.created != (prior is None)
        ):
            raise L5bPublicationError(
                "L5B_DEPLOY_OPERATION_MISMATCH",
                "Search publish result differs from attempted CAS mutation",
            )
        publish_applied = True
        created = mutation.created
        accounting.search_documents_written += len(compiled.documents)
        read_back = accounting.invoke(
            "post-publish-read-back",
            lambda: client.read_back(compiled.target_id),
        ).state
        if read_back is not None:
            accounting.search_documents_read += len(read_back.document_ids)
        _validate_state(
            read_back,
            _expected_state(compiled, token),
            phase="post-publish read-back",
        )
        accounting.require_complete()
        if accounting.search_calls > L5B_MAX_SUCCESS_SEARCH_CALLS:
            raise L5bPublicationError(
                "L5B_CALL_BUDGET_EXCEEDED",
                "successful publication exceeded the state-machine bound",
            )
        proofs = _projection_equivalences(compiled)
        _write_json(temp_root / "projection-equivalence.json", proofs)
        manifest = _artifact_manifest(compiled, proofs)
        _write_json(temp_root / "output-manifest.json", manifest)
        storage_write_bytes = sum(
            path.stat().st_size
            for path in temp_root.rglob("*")
            if path.is_file()
        )
        metrics = _metrics(
            compiled,
            started=started,
            cpu_started=cpu_started,
            rss_started=rss_started,
            accounting=accounting,
            storage_write_bytes=storage_write_bytes,
            cache_hits=0,
        )
        receipt = _receipt(
            compiled,
            status="succeeded",
            manifest=manifest,
            metrics=metrics,
            accounting=accounting,
            started_at=started_at,
        )
        _write_json(temp_root / "resource-metrics.json", metrics)
        _write_json(temp_root / "stage-receipt.json", receipt)
        _persist_checkpoint(
            run_root,
            compiled,
            manifest,
            metrics,
            receipt,
            checkpoint_signer,
        )
        if run_root.exists():
            shutil.rmtree(run_root)
        run_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_root, run_root)
        return L5bStageResult(
            compiled=compiled,
            projection_equivalences=proofs,
            output_manifest=manifest,
            metrics=metrics,
            receipt=receipt,
            run_root=run_root,
            reused=False,
        )
    except Exception as exc:
        cleanup_errors: list[str] = []
        try:
            if publish_started and not publish_applied:
                recovered = accounting.invoke(
                    "recovery-inspect",
                    lambda: client.inspect(compiled.target_id),
                ).state
                if recovered is not None:
                    accounting.search_documents_read += len(recovered.document_ids)
                publish_applied = (
                    recovered is not None and recovered.publication_token == token
                )
                created = publish_applied and prior is None
                if publish_applied:
                    accounting.search_documents_written += len(compiled.documents)
            if publish_applied:
                if created:
                    rollback = accounting.invoke(
                        "cleanup",
                        lambda: client.cleanup(
                            compiled.target_id,
                            publication_token=token,
                        ),
                    )
                else:
                    assert prior is not None
                    rollback = accounting.invoke(
                        "restore",
                        lambda: client.restore(
                            compiled.target_id,
                            prior_state=prior,
                            publication_token=token,
                        ),
                    )
                if (
                    rollback.target_id != compiled.target_id
                    or rollback.publication_token != token
                    or not rollback.applied
                ):
                    raise L5bPublicationError(
                        "L5B_ROLLBACK_OWNERSHIP_MISMATCH",
                        "Search rollback lost ownership fencing",
                    )
                accounting.search_documents_written += (
                    len(compiled.documents)
                    if created
                    else len(prior.document_ids)
                )
        except Exception as rollback_exc:
            cleanup_errors.append(str(rollback_exc))
        metrics = _metrics(
            compiled,
            started=started,
            cpu_started=cpu_started,
            rss_started=rss_started,
            accounting=accounting,
            storage_write_bytes=sum(
                path.stat().st_size
                for path in temp_root.rglob("*")
                if path.is_file()
            ),
            cache_hits=0,
        )
        code = exc.code if isinstance(exc, L5bPublicationError) else "L5B_PUBLICATION_FAILED"
        errors = [code, *accounting.error_codes]
        if cleanup_errors:
            errors.append("L5B_PARTIAL_CLEANUP_FAILED")
        receipt = _receipt(
            compiled,
            status="failed",
            manifest=None,
            metrics=metrics,
            accounting=accounting,
            started_at=started_at,
            error_codes=errors,
        )
        failure_root = state_root / "failures" / compiled.fingerprint
        failure_root.mkdir(parents=True, exist_ok=True)
        _write_json(failure_root / "resource-metrics.json", metrics)
        _write_json(failure_root / "stage-receipt.json", receipt)
        shutil.rmtree(temp_root, ignore_errors=True)
        raise L5bPublicationError(
            code,
            f"{exc}" + (f"; rollback failures: {cleanup_errors}" if cleanup_errors else ""),
            receipt=receipt,
            metrics=metrics,
        ) from exc


def require_l5b_publication_receipt(
    source: SealedL4ServingSource,
    l5a_result: L5aStageResult,
    result: L5bStageResult,
    *,
    checkpoint_integrity_signer: CheckpointIntegritySigner | None = None,
) -> None:
    """Authorize retrieval only from intact L5b artifacts and exact upstream seals."""

    require_l5a_publication_receipt(source, l5a_result)
    checkpoint_signer = _resolve_checkpoint_signer(checkpoint_integrity_signer)
    intact = _load_intact(result.compiled, result.run_root, checkpoint_signer)
    if (
        intact is None
        or result.compiled.source.receipt.receipt_hash != source.receipt.receipt_hash
        or result.compiled.l5a_result.output_manifest.manifest_hash
        != l5a_result.output_manifest.manifest_hash
        or result.receipt.status not in {"succeeded", "skipped"}
        or result.output_manifest != intact[0]
        or result.projection_equivalences != intact[1]
        or (
            result.receipt.status == "succeeded"
            and (
                result.receipt != intact[2]
                or result.metrics != intact[3]
            )
        )
        or (
            result.receipt.status == "skipped"
            and (
                checkpoint_signer is None
                or not _skip_checkpoint_is_valid(
                    result.run_root,
                    result.compiled,
                    result.metrics,
                    result.receipt,
                    checkpoint_signer,
                )
            )
        )
    ):
        raise L5bPublicationError(
            "L5B_PUBLICATION_RECEIPT_INVALID",
            "retrieval requires intact persisted L5b publication authority",
        )


def _agentic_runtime_seconds(max_runtime_milliseconds: int) -> int:
    seconds = max_runtime_milliseconds // 1000
    if seconds < 1:
        raise L5bPublicationError(
            "L5B_PROVIDER_TIMEOUT_UNREPRESENTABLE",
            "Azure agentic retrieval requires a positive whole-second timeout; "
            "the sealed QueryBudget cannot be rounded up",
        )
    return _provider_int32(seconds, field_name="maxRuntimeInSeconds")


# Azure Search REST request integer fields use signed int32 values.
_PROVIDER_INT32_MAX = (2 ** 31) - 1
_PROVIDER_INT32_MIN = -(2 ** 31)


def _provider_int32(value: int, *, field_name: str) -> int:
    if not 1 <= value <= _PROVIDER_INT32_MAX:
        raise L5bPublicationError(
            "L5B_PROVIDER_INTEGER_UNREPRESENTABLE",
            f"{field_name} must fit the provider signed int32 range",
        )
    return value


def _require_runtime_v1_1(
    context: AgenticRetrievalRequestContextV1_1,
    budget: QueryBudgetV1_1,
) -> None:
    if type(context) is not AgenticRetrievalRequestContextV1_1:
        raise ValueError(
            "schema2 L5b requires AgenticRetrievalRequestContext@1.1.0"
        )
    if type(budget) is not QueryBudgetV1_1:
        raise ValueError("schema2 L5b requires QueryBudget@1.1.0")
    context.validate_budget(budget)


@dataclass(frozen=True)
class _RetrievalBudgetShape:
    output_documents: int
    output_size: int
    runtime_seconds: int | None


def _shape_retrieval_budget(
    budget: QueryBudgetV1_1,
    *,
    retrieval_mode: str,
) -> _RetrievalBudgetShape:
    """Map sealed C0 ceilings to provider-enforceable request values."""

    output_documents = min(
        budget.max_output_documents,
        budget.max_search_result_records,
    )
    output_documents = _provider_int32(
        output_documents,
        field_name="maxOutputDocuments",
    )
    if retrieval_mode == "agentic_preview":
        # 2026-05-01-preview exposes no independent source-call or subquery
        # limit. One explicit intent targeted to one source is the exact
        # provider-enforceable minimum and bypasses model query planning.
        if budget.max_agentic_internal_subqueries < 1:
            raise L5bPublicationError(
                "L5B_AGENTIC_SUBQUERY_BUDGET_UNAVAILABLE",
                "agentic retrieval requires exactly one explicit subquery, but "
                "the sealed subquery budget is zero",
            )
        if budget.max_agentic_source_calls < 1:
            raise L5bPublicationError(
                "L5B_AGENTIC_SOURCE_CALL_BUDGET_UNAVAILABLE",
                "agentic retrieval requires exactly one targeted source call, but "
                "the sealed source-call budget is zero",
            )
        return _RetrievalBudgetShape(
            output_documents=output_documents,
            output_size=_provider_int32(
                budget.max_output_tokens,
                field_name="maxOutputSize",
            ),
            runtime_seconds=_agentic_runtime_seconds(
                budget.max_runtime_milliseconds
            ),
        )
    if retrieval_mode == "direct_hybrid_prefilter":
        if budget.max_direct_search_requests < 1:
            raise L5bPublicationError(
                "L5B_DIRECT_SEARCH_BUDGET_UNAVAILABLE",
                "direct retrieval is unavailable under the sealed request budget",
            )
        return _RetrievalBudgetShape(
            output_documents=output_documents,
            output_size=budget.max_output_tokens,
            runtime_seconds=None,
        )
    raise ValueError(f"unsupported retrieval mode: {retrieval_mode}")


def build_agentic_retrieve_payload(
    context: AgenticRetrievalRequestContextV1_1,
    budget: QueryBudgetV1_1,
    scope: ResolvedRetrievalScope,
    *,
    query_text: str,
    filter_add_on: str,
) -> Mapping[str, Any]:
    """Build the pinned preview extractive request; fabric-kg adds no prompt."""

    _require_runtime_v1_1(context, budget)
    context.validate_scope(scope)
    if context.retrieval_mode != "agentic_preview":
        raise ValueError("agentic payload requires agentic_preview context")
    if context.retrieval_reasoning_effort not in {"minimal", "low", "medium"}:
        raise ValueError("unsupported retrieval reasoning effort")
    expected_filter = canonical_scope_filter(
        canonical_entity_ids=context.filter_add_on.canonical_entity_ids,
        canonical_type_ids=context.filter_add_on.exact_type_ids,
        canonical_relationship_ids=context.filter_add_on.canonical_relationship_ids,
        access_policy_hash=context.acl_scope_hash,
        asserted_publication_hash=context.asserted_publication_hash,
    )
    if filter_add_on != expected_filter:
        raise ValueError("filterAddOn differs from locally hashed canonical scope")
    shape = _shape_retrieval_budget(
        budget,
        retrieval_mode=context.retrieval_mode,
    )
    return {
        "intents": [{"type": "semantic", "search": query_text}],
        "knowledgeSourceParams": [{
            "knowledgeSourceName": context.knowledge_source_id,
            "kind": "searchIndex",
            "filterAddOn": filter_add_on,
            "includeReferences": True,
            "includeReferenceSourceData": True,
            "maxOutputDocuments": shape.output_documents,
            "failOnError": True,
        }],
        "outputMode": "extractiveData",
        "retrievalReasoningEffort": {
            "kind": context.retrieval_reasoning_effort,
        },
        "maxRuntimeInSeconds": shape.runtime_seconds,
        "maxOutputSize": shape.output_size,
        "maxOutputDocuments": shape.output_documents,
        "includeActivity": context.request_activity,
    }


def build_direct_search_payload(
    context: AgenticRetrievalRequestContextV1_1,
    budget: QueryBudgetV1_1,
    scope: ResolvedRetrievalScope,
    *,
    query_text: str,
    vector: Sequence[float] | None,
    vector_available: bool = False,
    originating_context: AgenticRetrievalRequestContextV1_1 | None = None,
    originating_budget: QueryBudgetV1_1 | None = None,
) -> "L5bDirectSearchRequest":
    """Build one stable direct filtered query with explicit vector degradation."""

    _require_runtime_v1_1(context, budget)
    context.validate_scope(scope)
    if context.retrieval_mode != "direct_hybrid_prefilter":
        raise ValueError("direct payload requires direct_hybrid_prefilter context")
    fallback_declared = context.fallback_for_request_context_id is not None
    fallback_supplied = (
        originating_context is not None or originating_budget is not None
    )
    if fallback_declared or fallback_supplied:
        if originating_context is None or originating_budget is None:
            raise L5bPublicationError(
                "L5B_DIRECT_FALLBACK_UNAUTHORIZED",
                "direct fallback requires its originating context and budget",
            )
        originating_context.validate_budget(originating_budget)
        context.validate_fallback_origin(originating_context)
    shape = _shape_retrieval_budget(
        budget,
        retrieval_mode=context.retrieval_mode,
    )
    filter_text = canonical_scope_filter(
        canonical_entity_ids=context.canonical_entity_ids,
        canonical_type_ids=context.exact_type_ids,
        canonical_relationship_ids=context.graph_scope_filter.canonical_relationship_ids,
        access_policy_hash=context.acl_scope_hash,
        asserted_publication_hash=context.asserted_publication_hash,
    )
    payload: dict[str, Any] = {
        "search": query_text,
        "filter": filter_text,
        "queryType": "semantic",
        "semanticConfiguration": "evidence-semantic",
        "top": shape.output_documents,
        "select": ",".join(_SOURCE_DATA_FIELDS),
        "count": True,
    }
    degradation_code = None
    if vector is not None and vector_available:
        if budget.max_vector_search_requests < 1:
            raise L5bPublicationError(
                "L5B_VECTOR_SEARCH_BUDGET_UNAVAILABLE",
                "vector retrieval is unavailable under the sealed request budget",
            )
        if (
            len(vector) != 1536
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                for value in vector
            )
        ):
            raise ValueError(
                "sealed evidence vectors must contain 1536 finite numeric values"
            )
        payload["vectorFilterMode"] = "preFilter"
        payload["vectorQueries"] = [{
            "kind": "vector",
            "vector": list(vector),
            "fields": "vector",
            "k": shape.output_documents,
        }]
    else:
        degradation_code = "vector_unavailable_keyword_semantic_filtered"
    return L5bDirectSearchRequest(
        payload=payload,
        degradation_code=degradation_code,
    )


@dataclass(frozen=True)
class L5bDirectSearchRequest:
    """Official Search payload plus local degradation evidence."""

    payload: Mapping[str, Any]
    degradation_code: str | None


@dataclass(frozen=True)
class L5bRetrievalResult:
    citations: tuple[SearchCitationEnvelope, ...]
    presentations: tuple[CitationPresentation, ...]
    coverage: AgenticRetrievalCoverageReceiptV1_1


def _citation_identity(
    context: AgenticRetrievalRequestContextV1_1,
    document: Mapping[str, Any],
    contract_kind: str,
) -> CanonicalIdentityEnvelope:
    values = context.identity.model_dump(mode="python", round_trip=True)
    locator_model = _parse_immutable_locator(document["immutable_locator_json"])
    locator = locator_model.model_dump(mode="json")
    if (
        document.get("immutable_locator_hash") != locator_model.locator_hash
        or document.get("page") != locator_model.page
    ):
        raise ValueError("Search document locator duplicates differ from authority")
    values.update({
        "contract_kind": contract_kind,
        "contract_version": "1.0.0",
        "asset_id": document["asset_id"],
        "asset_version_id": document["asset_version_id"],
        "source_file_id": document["source_file_id"],
        "source_unit_id": document["source_unit_id"],
        "content_hash": document["source_text_content_hash"],
        "immutable_locator": locator,
    })
    return CanonicalIdentityEnvelope.model_validate(values)


def build_citation(
    context: AgenticRetrievalRequestContextV1_1,
    *,
    reference_id: str,
    document: Mapping[str, Any],
) -> tuple[SearchCitationEnvelope, CitationPresentation]:
    """Normalize one exact policy-approved Search document into C0.Runtime."""

    required = (
        "id",
        "document_hash",
        "original_document_name",
        "source_id",
        "source_file_id",
        "asset_id",
        "asset_version_id",
        "asset_hash",
        "source_unit_id",
        "source_text_content_hash",
        "evidence_span_ids",
        "canonical_entity_ids",
        "canonical_assertion_ids",
        "source_quote",
        "source_quote_is_verbatim",
        "quote_hash",
        "immutable_locator_json",
        "access_policy_id",
        "access_policy_hash",
    )
    if any(name not in document for name in required):
        raise ValueError("Search document omitted required citation lineage")
    if (
        document["source_quote_is_verbatim"] is not True
        or canonical_sha256({
            key: value for key, value in document.items() if key != "document_hash"
        }) != document["document_hash"]
        or document["access_policy_hash"] != context.acl_scope_hash
        or document["asserted_publication_hash"] != context.asserted_publication_hash
    ):
        raise ValueError("Search document quote, policy, or publication seal is stale")
    locator = json.loads(str(document["immutable_locator_json"]))
    _safe_source_display_name(
        document["original_document_name"],
        source_file_id=document["source_file_id"],
    )
    locator_section_path = _safe_section_path(
        locator.get("section_path") or (),
        source_unit_id=document["source_unit_id"],
    )
    document_section_path = _safe_section_path(
        document.get("section_path") or (),
        source_unit_id=document["source_unit_id"],
    )
    if document_section_path != locator_section_path:
        raise ValueError("Search document section path differs from immutable locator")
    identity = _citation_identity(
        context,
        document,
        "c0.search_citation_envelope",
    )
    citation_values = {
        "identity": identity,
        "search_citation_envelope_id": deterministic_contract_id(
            "search-citation-envelope",
            {
                "context": context.request_context_hash,
                "reference": reference_id,
                "document": document["document_hash"],
            },
        ),
        "search_reference_id": reference_id,
        "search_document_id": document["id"],
        "original_document_name": document["original_document_name"],
        "source_id": document["source_id"],
        "source_file_id": document["source_file_id"],
        "source_unit_id": document["source_unit_id"],
        "chunk_id": document["id"],
        "evidence_span_ids": tuple(document["evidence_span_ids"]),
        "canonical_scope_id": context.resolved_retrieval_scope_id,
        "canonical_entity_ids": tuple(document["canonical_entity_ids"]),
        "canonical_relationship_ids": tuple(document["canonical_relationship_ids"]),
        "canonical_assertion_ids": tuple(document["canonical_assertion_ids"]),
        "exact_authorized_quote": document["source_quote"],
        "quote_hash": document["quote_hash"],
        "page": locator.get("page"),
        "section_path": locator_section_path,
        "immutable_locator": locator,
        "content_hash": document["source_text_content_hash"],
        "asset_hash": document["asset_hash"],
        "access_policy_id": document["access_policy_id"],
        "access_policy_hash": document["access_policy_hash"],
        "governed_asset_reference_id": document.get("governed_asset_reference_id"),
        "governed_asset_reference_hash": document.get(
            "governed_asset_reference_hash"
        ),
    }
    citation = SearchCitationEnvelope(
        **citation_values,
        citation_hash=canonical_sha256(citation_values),
    )
    presentation_identity = _citation_identity(
        context,
        document,
        "c0.citation_presentation",
    )
    presentation_values = {
        "identity": presentation_identity,
        "citation_presentation_id": deterministic_contract_id(
            "citation-presentation",
            {"citation": citation.citation_hash},
        ),
        "search_citation_envelope_id": citation.search_citation_envelope_id,
        "search_citation_envelope_hash": citation.citation_hash,
        "original_document_name": citation.original_document_name,
        "source_id": citation.source_id,
        "source_file_id": citation.source_file_id,
        "source_unit_id": citation.source_unit_id,
        "chunk_id": citation.chunk_id,
        "evidence_span_ids": citation.evidence_span_ids,
        "exact_authorized_quote": citation.exact_authorized_quote,
        "quote_hash": citation.quote_hash,
        "page": citation.page,
        "section_path": citation.section_path,
        "immutable_locator": citation.immutable_locator,
        "content_hash": citation.content_hash,
        "asset_hash": citation.asset_hash,
        "governed_asset_reference_id": citation.governed_asset_reference_id,
        "governed_asset_reference_hash": citation.governed_asset_reference_hash,
    }
    presentation = CitationPresentation(
        **presentation_values,
        presentation_hash=canonical_sha256(presentation_values),
    )
    presentation.validate_citation(citation)
    return citation, presentation


def _warning_codes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple)) else [value]
    return tuple(sorted({
        f"provider-warning:{canonical_sha256(item)[:32]}"
        for item in items
    }))


def _opaque_external_id(prefix: str, value: Any) -> str:
    return f"{prefix}:{canonical_sha256(value)[:32]}"


def _provider_activity_key(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        if _PROVIDER_INT32_MIN <= value <= _PROVIDER_INT32_MAX:
            return f"integer:{value}"
    raise L5bPublicationError(
        "L5B_REMOTE_ACCOUNTING_CONTRADICTORY",
        "provider Search activity identity is invalid",
    )


def _response_items(
    context: AgenticRetrievalRequestContextV1_1,
    response: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    if context.retrieval_mode == "direct_hybrid_prefilter":
        values = response.get("value")
        if not isinstance(values, list):
            raise ValueError("direct Search response omitted value documents")
        references = []
        for index, item in enumerate(values):
            if not isinstance(item, Mapping):
                raise ValueError("direct Search result document is malformed")
            document = {
                key: value
                for key, value in item.items()
                if not str(key).startswith("@search.")
            }
            references.append({
                "id": f"direct-reference:{index}",
                "sourceData": document,
            })
        return references, []
    references = response.get("references")
    activity = response.get("activity")
    if not isinstance(references, list) or not isinstance(activity, list):
        raise ValueError("agentic response omitted references or activity")
    return references, activity


def _strict_json_object(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be canonical JSON text")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"{field_name} contains duplicate key {key!r}")
            result[key] = item
        return result

    parsed = json.loads(value, object_pairs_hook=object_pairs)
    if not isinstance(parsed, Mapping):
        raise TypeError(f"{field_name} must contain a JSON object")
    return parsed


def _parse_immutable_locator(value: object) -> ImmutableSourceLocator:
    parsed = _strict_json_object(value, field_name="immutable_locator_json")
    expected_fields = set(ImmutableSourceLocator.model_fields)
    if set(parsed) != expected_fields:
        raise ValueError(
            "immutable_locator_json fields differ from the exact locator schema"
        )
    section_path = parsed.get("section_path")
    if section_path is not None and (
        not isinstance(section_path, list)
        or any(not isinstance(item, str) for item in section_path)
    ):
        raise TypeError("immutable locator section_path must be an array of strings")
    locator = ImmutableSourceLocator.model_validate(parsed)
    if locator.model_dump(mode="json") != parsed:
        raise ValueError("immutable locator canonical types changed during validation")
    return locator


def _document_matches_sealed_payload(
    document: Mapping[str, Any],
    sealed: Mapping[str, Any],
) -> bool:
    expected_fields = set(_SOURCE_DATA_FIELDS)
    if set(document) != expected_fields or set(sealed) != expected_fields:
        return False
    returned_hash = document.get("document_hash")
    recomputed_hash = canonical_sha256({
        key: value
        for key, value in document.items()
        if key != "document_hash"
    })
    return (
        isinstance(returned_hash, str)
        and returned_hash == recomputed_hash
        and returned_hash == sealed.get("document_hash")
        and canonical_json(document) == canonical_json(sealed)
    )


def _document_scope_findings(
    document: Mapping[str, Any],
    context: AgenticRetrievalRequestContextV1_1,
    ontology_scope: ResolvedOntologyScope,
    publication: L5bStageResult,
) -> tuple[Mapping[str, tuple[str, ...]], tuple[str, ...]]:
    findings: dict[str, set[str]] = {}

    def add(
        reason: str,
        values: Sequence[Any] = (),
        *,
        dimension: str | None = None,
    ) -> None:
        normalized = {
            str(item) for item in values if item is not None
        }
        if not normalized:
            normalized.add(
                f"document:{document.get('id', 'missing')}:"
                f"dimension:{dimension or reason}"
            )
        findings.setdefault(reason, set()).update(normalized)

    allowed_entities = set(context.canonical_entity_ids)
    document_entities = {
        str(item) for item in document.get("canonical_entity_ids", ())
    }
    unexpected_entities = tuple(sorted(document_entities - allowed_entities))
    if not document_entities or unexpected_entities:
        add(
            "unexpected_member" if unexpected_entities else "scope_key_missing",
            unexpected_entities or (document.get("id"),),
        )

    allowed_relationships = set(
        context.graph_scope_filter.canonical_relationship_ids
    )
    document_relationships = {
        str(item) for item in document.get("canonical_relationship_ids", ())
    }
    unexpected_relationships = document_relationships - allowed_relationships
    if not document_relationships:
        add("scope_key_missing", dimension="canonical_relationship_ids")
    elif unexpected_relationships:
        add("unknown_relationship", unexpected_relationships)

    allowed_types = set(context.exact_type_ids).union(context.ancestor_type_ids)
    document_types = {
        str(item) for item in document.get("canonical_type_ids", ())
    }
    unexpected_types = document_types - allowed_types
    if not document_types:
        add("scope_key_missing", dimension="canonical_type_ids")
    elif unexpected_types:
        add("hierarchy_scope_mismatch", unexpected_types)

    document_properties = tuple(
        str(item) for item in document.get("canonical_property_ids", ())
    )
    if document_properties:
        # C0.Runtime 1.0 has no resolved property-scope carrier. Property evidence
        # therefore cannot be exposed without a stronger scope authority.
        add("scope_key_missing", document_properties)

    allowed_assertions = {
        *ontology_scope.assertion_ids,
        *(item.type_assertion_id for item in ontology_scope.type_assertions),
        *(
            assertion_id
            for item in ontology_scope.members
            for assertion_id in item.membership_assertion_ids
        ),
        *(
            edge.relationship_assertion_id
            for edge in ontology_scope.adjacency_edges
        ),
    }
    document_assertions = {
        str(item) for item in document.get("canonical_assertion_ids", ())
    }
    if not document_assertions or not document_assertions <= allowed_assertions:
        add(
            "citation_invalid",
            document_assertions - allowed_assertions
            or (document.get("id"),),
        )

    manifest_ids = {
        str(item)
        for item in document.get("required_member_manifest_ids", ())
    }
    expected_manifest_id = (
        ontology_scope.required_member_manifest.required_member_manifest_id
    )
    if manifest_ids != {expected_manifest_id}:
        add(
            "collection_hash_mismatch",
            (*manifest_ids, expected_manifest_id),
            dimension="required_member_manifest_ids",
        )

    policy = publication.compiled.access_policy
    expected_principals = _principal_keys(policy)
    expected_scopes = _scope_keys(policy)
    actual_principals = tuple(sorted(document.get("acl_principal_keys", ())))
    actual_scopes = tuple(sorted(document.get("acl_scope_keys", ())))
    acl_values = (
        document.get("access_policy_id"),
        document.get("access_policy_hash"),
        document.get("authorization_resource_id"),
        *actual_principals,
        *actual_scopes,
        policy.access_policy_id,
        context.acl_scope_hash,
        policy.authorization_resource_id,
        *expected_principals,
        *expected_scopes,
    )
    if (
        document.get("access_policy_id") != policy.access_policy_id
        or document.get("access_policy_hash") != context.acl_scope_hash
        or document.get("access_policy_hash") != policy.policy_hash
        or document.get("authorization_resource_id")
        != policy.authorization_resource_id
        or actual_principals != expected_principals
        or actual_scopes != expected_scopes
    ):
        add("citation_unauthorized", acl_values)

    if document.get("source_id") != document.get("source_file_id"):
        add(
            "citation_invalid",
            (document.get("source_id"), document.get("source_file_id")),
            dimension="source_file_id",
        )
    try:
        _safe_source_display_name(
            document.get("original_document_name"),
            source_file_id=str(document.get("source_file_id") or "missing"),
        )
    except L5bPublicationError:
        add("citation_invalid", dimension="original_document_name")
    try:
        locator_model = _parse_immutable_locator(
            document.get("immutable_locator_json")
        )
        locator_section_path = _safe_section_path(
            locator_model.section_path or (),
            source_unit_id=str(document.get("source_unit_id") or "missing"),
        )
        document_section_path = _safe_section_path(
            document.get("section_path") or (),
            source_unit_id=str(document.get("source_unit_id") or "missing"),
        )
        if document_section_path != locator_section_path:
            add("citation_invalid", dimension="section_path")
        if (
            document.get("immutable_locator_hash") != locator_model.locator_hash
            or document.get("page") != locator_model.page
        ):
            add("citation_invalid", dimension="immutable_locator")
    except (
        L5bPublicationError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        add("citation_invalid", dimension="section_path")
    governed_assets = {
        (
            asset.governed_asset_reference_id,
            asset.asset_reference_hash,
            asset.source_file_id,
            asset.asset_id,
            asset.asset_version_id,
            asset.content_hash,
        )
        for asset in publication.compiled.governed_assets
    }
    document_asset = (
        document.get("governed_asset_reference_id"),
        document.get("governed_asset_reference_hash"),
        document.get("source_file_id"),
        document.get("asset_id"),
        document.get("asset_version_id"),
        document.get("asset_hash"),
    )
    if document_asset not in governed_assets:
        matching_assets = tuple(
            value
            for asset in governed_assets
            if asset[2] == document.get("source_file_id")
            for value in asset
        )
        add(
            "citation_invalid",
            (*document_asset, *matching_assets),
            dimension="governed_asset",
        )

    if document.get("asserted_publication_hash") != context.asserted_publication_hash:
        add(
            "projection_hash_stale",
            (
                document.get("asserted_publication_hash"),
                context.asserted_publication_hash,
            ),
            dimension="asserted_publication_hash",
        )

    compiled_l5a = getattr(publication.compiled, "l5a_result", None)
    if compiled_l5a is not None:
        upstream_checks = (
            (
                "l3_artifact_manifest_hash",
                compiled_l5a.compiled.source.input_manifest.manifest_hash,
            ),
            (
                "l4_projection_hash",
                compiled_l5a.compiled.source.projection.projection_hash,
            ),
            (
                "l4_receipt_hash",
                compiled_l5a.compiled.source.receipt.receipt_hash,
            ),
            (
                "l5a_publication_fingerprint",
                compiled_l5a.compiled.fingerprint,
            ),
            ("l5a_receipt_hash", compiled_l5a.receipt.receipt_hash),
        )
        for field_name, expected_value in upstream_checks:
            if document.get(field_name) != expected_value:
                add(
                    "projection_hash_stale",
                    (document.get(field_name), expected_value),
                    dimension=field_name,
                )
        expected_crosswalks = tuple(sorted(
            item.crosswalk_hash for item in compiled_l5a.compiled.crosswalks
        ))
        actual_crosswalks = tuple(sorted(
            document.get("publication_crosswalk_hashes", ())
        ))
        if actual_crosswalks != expected_crosswalks:
            add(
                "crosswalk_hash_stale",
                (*actual_crosswalks, *expected_crosswalks),
                dimension="publication_crosswalk_hashes",
            )

    return {
        reason: tuple(sorted(values))
        for reason, values in sorted(findings.items())
    }, unexpected_entities


def interpret_retrieval_response(
    context: AgenticRetrievalRequestContextV1_1,
    budget: QueryBudgetV1_1,
    ontology_scope: ResolvedOntologyScope,
    retrieval_scope: ResolvedRetrievalScope,
    *,
    publication: L5bStageResult,
    response: Mapping[str, Any],
    accounting: L5bRemoteAccounting,
    checkpoint_integrity_signer: CheckpointIntegritySigner | None = None,
    originating_context: AgenticRetrievalRequestContextV1_1 | None = None,
    originating_budget: QueryBudgetV1_1 | None = None,
    fallback_reason_code: str | None = None,
    degradation_code: str | None = None,
) -> L5bRetrievalResult:
    """Return exact evidence and bounded coverage; never answer or summarize."""

    _require_runtime_v1_1(context, budget)
    _validate_remote_accounting(accounting)
    if context.retrieval_mode.startswith("agentic_") and (
        accounting.vector_search_requests
        or accounting.embedding_calls
        or accounting.embedding_items
    ):
        raise L5bPublicationError(
            "L5B_REMOTE_ACCOUNTING_CONTRADICTORY",
            "agentic retrieval reported direct vector or embedding observations",
        )
    require_l5b_publication_receipt(
        publication.compiled.source,
        publication.compiled.l5a_result,
        publication,
        checkpoint_integrity_signer=checkpoint_integrity_signer,
    )
    if (
        context.search_index_id != publication.compiled.index_name
        or context.knowledge_source_id
        != publication.compiled.knowledge_source_name
        or context.knowledge_base_id != publication.compiled.knowledge_base_name
        or context.search_index_fingerprint
        != publication.compiled.index_fingerprint
    ):
        raise ValueError("retrieval context differs from sealed L5b resources")
    sealed_documents = {
        str(document["id"]): document
        for document in publication.compiled.documents
    }
    if (
        len(sealed_documents) != len(publication.compiled.documents)
        or tuple(sorted(
            (document_id, str(document["document_hash"]))
            for document_id, document in sealed_documents.items()
        ))
        != tuple(sorted(publication.compiled.document_hashes))
    ):
        raise ValueError("sealed L5b document authority is internally inconsistent")
    retrieval_scope.validate_resolved_scope(ontology_scope)
    context.validate_scope(retrieval_scope)
    if originating_context is not None:
        if originating_budget is None:
            raise ValueError("fallback retrieval requires its originating 1.1 budget")
        _require_runtime_v1_1(originating_context, originating_budget)
        context.validate_fallback_origin(originating_context)
    if degradation_code is not None and context.retrieval_mode != "direct_hybrid_prefilter":
        raise ValueError("only direct retrieval can report vector degradation")
    references, activity_raw = _response_items(context, response)
    search_activities = tuple(
        item
        for item in activity_raw
        if isinstance(item, Mapping) and item.get("type") == "searchIndex"
    )
    search_activity_keys = tuple(
        _provider_activity_key(item.get("id"))
        for item in search_activities
    )
    if len(search_activity_keys) != len(set(search_activity_keys)):
        raise L5bPublicationError(
            "L5B_REMOTE_ACCOUNTING_CONTRADICTORY",
            "provider response contains duplicate Search activity IDs",
        )
    search_activity_ids = set(search_activity_keys)
    citations: list[SearchCitationEnvelope] = []
    presentations: list[CitationPresentation] = []
    missing_reference_ids: list[str] = []
    quarantined_findings: dict[str, set[str]] = {}
    quarantined_unexpected_ids: set[str] = set()
    returned_document_type_ids: set[str] = set()
    verified_references_by_activity: dict[str, int] = {}
    reference_id_counts: dict[str, int] = {}
    document_id_counts: dict[str, int] = {}
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        reference_id = reference.get("id") or reference.get("ref_id")
        document = reference.get("sourceData") or reference.get("source_data")
        if isinstance(reference_id, str):
            reference_id_counts[reference_id] = (
                reference_id_counts.get(reference_id, 0) + 1
            )
        if isinstance(document, Mapping) and isinstance(document.get("id"), str):
            document_id = str(document["id"])
            document_id_counts[document_id] = (
                document_id_counts.get(document_id, 0) + 1
            )
    duplicate_reference_ids = {
        value for value, count in reference_id_counts.items() if count > 1
    }
    duplicate_document_ids = {
        value for value, count in document_id_counts.items() if count > 1
    }
    for index, reference in enumerate(references):
        local_reference_id = f"search-reference:{index}"
        if not isinstance(reference, Mapping):
            missing_reference_ids.append(local_reference_id)
            quarantined_findings.setdefault("citation_invalid", set())
            continue
        returned_reference_id = reference.get("id") or reference.get("ref_id")
        document = reference.get("sourceData") or reference.get("source_data")
        if not isinstance(returned_reference_id, str) or not isinstance(document, Mapping):
            missing_reference_ids.append(local_reference_id)
            quarantined_findings.setdefault("citation_invalid", set())
            continue
        document_id = str(document.get("id") or "document:missing")
        if (
            returned_reference_id in duplicate_reference_ids
            or document_id in duplicate_document_ids
        ):
            missing_reference_ids.append(local_reference_id)
            safe_document_id = (
                document_id if document_id in sealed_documents else None
            )
            finding_ids = quarantined_findings.setdefault(
                "citation_invalid",
                set(),
            )
            if safe_document_id is not None:
                finding_ids.add(safe_document_id)
            continue
        if (
            context.retrieval_mode.startswith("agentic_")
            and _provider_activity_key(reference.get("activitySource"))
            not in search_activity_ids
        ):
            missing_reference_ids.append(local_reference_id)
            quarantined_findings.setdefault("citation_invalid", set())
            continue
        sealed_document = sealed_documents.get(document_id)
        if (
            sealed_document is None
            or not _document_matches_sealed_payload(document, sealed_document)
        ):
            missing_reference_ids.append(local_reference_id)
            finding_ids = quarantined_findings.setdefault(
                "citation_invalid",
                set(),
            )
            if sealed_document is not None:
                finding_ids.add(str(sealed_document["id"]))
            continue
        scope_findings, scope_unexpected_ids = _document_scope_findings(
            document,
            context,
            ontology_scope,
            publication,
        )
        if scope_findings:
            missing_reference_ids.append(local_reference_id)
            for reason, values in scope_findings.items():
                quarantined_findings.setdefault(reason, set()).update(values)
            quarantined_unexpected_ids.update(scope_unexpected_ids)
            continue
        try:
            citation, presentation = build_citation(
                context,
                reference_id=local_reference_id,
                document=document,
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            missing_reference_ids.append(local_reference_id)
            quarantined_findings.setdefault("citation_invalid", set()).add(
                str(sealed_document["id"])
            )
            continue
        citations.append(citation)
        presentations.append(presentation)
        activity_source = reference.get("activitySource")
        if activity_source is not None:
            activity_key = _provider_activity_key(activity_source)
            verified_references_by_activity[activity_key] = (
                verified_references_by_activity.get(activity_key, 0) + 1
            )
        returned_document_type_ids.update(
            str(item) for item in document.get("canonical_type_ids", ())
        )

    by_entity: dict[str, list[SearchCitationEnvelope]] = {}
    raw_occurrences: dict[str, int] = {}
    for citation in citations:
        for entity_id in citation.canonical_entity_ids:
            raw_occurrences[entity_id] = raw_occurrences.get(entity_id, 0) + 1
            by_entity.setdefault(entity_id, []).append(citation)
    required_ids = tuple(retrieval_scope.canonical_member_ids)
    returned_ids = tuple(sorted(set(by_entity).intersection(required_ids)))
    unexpected_ids = tuple(sorted(set(by_entity) - set(required_ids)))
    duplicate_ids = tuple(sorted(
        entity_id
        for entity_id, count in raw_occurrences.items()
        if count > 1 and len({
            item.search_document_id for item in by_entity[entity_id]
        }) != count
    ))
    member_authority = {
        item.canonical_entity_id: item for item in ontology_scope.members
    }
    returned_members = []
    citation_mappings = []
    for entity_id in returned_ids:
        authoritative = member_authority[entity_id]
        entity_citations = by_entity[entity_id]
        reference_ids = tuple(sorted({
            item.search_reference_id for item in entity_citations
        }))
        citation_ids = tuple(sorted({
            item.search_citation_envelope_id for item in entity_citations
        }))
        member_values = {
            "canonical_entity_id": entity_id,
            "canonical_semantic_type_id": authoritative.canonical_semantic_type_id,
            "member_role_id": authoritative.member_role_id,
            "group_id": authoritative.group_id,
            "sequence_position": authoritative.sequence_position,
            "search_reference_ids": reference_ids,
            "search_citation_envelope_ids": citation_ids,
        }
        returned_members.append(CoverageMemberReference(
            **member_values,
            member_hash=canonical_sha256(member_values),
        ))
        citation_mappings.extend(
            CitationCanonicalMapping(
                canonical_entity_id=entity_id,
                search_reference_id=item.search_reference_id,
                search_citation_envelope_id=item.search_citation_envelope_id,
                search_citation_envelope_hash=item.citation_hash,
            )
            for item in entity_citations
        )
    returned_member_tuple = tuple(returned_members)
    returned_roles = tuple(sorted({
        item.member_role_id
        for item in returned_member_tuple
        if item.member_role_id is not None
    }))
    returned_group_hash = (
        canonical_sha256(sorted(
            (item.canonical_entity_id, item.group_id)
            for item in returned_member_tuple
            if item.group_id is not None
        ))
        if any(item.group_id is not None for item in returned_member_tuple)
        else None
    )
    returned_sequence_hash = (
        canonical_sha256([
            (item.sequence_position, item.canonical_entity_id)
            for item in sorted(
                returned_member_tuple,
                key=lambda value: (
                    value.sequence_position
                    if value.sequence_position is not None
                    else -1
                ),
            )
        ])
        if retrieval_scope.sequence_hash is not None
        else None
    )
    returned_exact_type_ids = tuple(sorted(
        set(context.exact_type_ids).intersection(returned_document_type_ids)
    ))
    returned_ancestor_type_ids = tuple(sorted(
        set(context.ancestor_type_ids).intersection(returned_document_type_ids)
    ))
    activity = tuple(
        ActivityReceipt(
            activity_id=f"activity:{index}",
            activity_kind=(
                str(item.get("type"))
                if item.get("type") in {
                    "searchIndex",
                    "modelQueryPlanning",
                    "agenticReasoning",
                    "modelAnswerSynthesis",
                    "modelWebSummarization",
                    "imageServing",
                }
                else "providerActivity"
            ),
            activity_hash=canonical_sha256(item),
            warning_codes=_warning_codes(
                item.get("warning") or item.get("warnings")
            ),
            truncated=bool(
                item.get("truncated", False)
                or item.get("warningCode") == "outputTruncated"
            ),
        )
        for index, item in enumerate(activity_raw)
    )
    if context.retrieval_mode.startswith("agentic_") and not search_activities:
        raise ValueError("agentic response omitted searchIndex activity")
    if context.retrieval_mode.startswith("agentic_"):
        candidate_observation = 0
        for item in search_activities:
            matched_count = item.get("count")
            if (
                not isinstance(matched_count, int)
                or isinstance(matched_count, bool)
                or not 0 <= matched_count <= _PROVIDER_INT32_MAX
                or candidate_observation > _PROVIDER_INT32_MAX - matched_count
            ):
                raise L5bPublicationError(
                    "L5B_REMOTE_ACCOUNTING_CONTRADICTORY",
                    "provider activity candidate accounting is invalid",
                )
            candidate_observation += matched_count
        if candidate_observation != accounting.candidate_count:
            raise L5bPublicationError(
                "L5B_REMOTE_ACCOUNTING_CONTRADICTORY",
                "provider activity and adapter candidate accounting disagree",
            )
    else:
        direct_provider_count = response.get(
            "@odata.count",
            accounting.candidate_count,
        )
        if (
            not isinstance(direct_provider_count, int)
            or isinstance(direct_provider_count, bool)
            or not 0 <= direct_provider_count <= _PROVIDER_INT32_MAX
            or direct_provider_count != accounting.candidate_count
        ):
            raise L5bPublicationError(
                "L5B_REMOTE_ACCOUNTING_CONTRADICTORY",
                "direct provider and adapter candidate accounting disagree",
            )
        candidate_observation = direct_provider_count
    verified_document_count = len(citations)
    if candidate_observation < verified_document_count:
        raise L5bPublicationError(
            "L5B_REMOTE_ACCOUNTING_CONTRADICTORY",
            "provider candidate count is below verified returned documents",
        )
    source_calls_list: list[SourceCallReceipt] = []
    for item, activity_key in zip(search_activities, search_activity_keys, strict=True):
        matched_count = item["count"]
        returned_count = (
            0
            if item.get("error") is not None
            else verified_references_by_activity.get(activity_key, 0)
        )
        if returned_count > matched_count:
            raise L5bPublicationError(
                "L5B_REMOTE_ACCOUNTING_CONTRADICTORY",
                "provider activity matched count is below verified returns",
            )
        source_calls_list.append(SourceCallReceipt(
            source_call_id=_opaque_external_id("source-call", activity_key),
            knowledge_source_id=context.knowledge_source_id,
            request_hash=canonical_sha256(
                item.get("searchIndexArguments")
                or {
                    "context": context.request_context_hash,
                    "activity": activity_key,
                }
            ),
            response_hash=(
                None if item.get("error") is not None else canonical_sha256(item)
            ),
            status=(
                "failed"
                if item.get("error") is not None
                else "partial"
                if _warning_codes(item.get("warning") or item.get("warnings"))
                else "succeeded"
            ),
            matched_count=matched_count,
            returned_count=returned_count,
        ))
    source_calls = tuple(source_calls_list)
    if not source_calls:
        source_calls = (
            SourceCallReceipt(
                source_call_id=f"source-call:{context.request_context_id}",
                knowledge_source_id=context.knowledge_source_id,
                request_hash=context.request_context_hash,
                response_hash=canonical_sha256(response),
                status="partial" if accounting.warning_codes else "succeeded",
                matched_count=candidate_observation,
                returned_count=verified_document_count,
            ),
        )
    planned = tuple(
        PlannedSubqueryReceipt(
            subquery_id=_opaque_external_id("subquery", activity_key),
            subquery_hash=canonical_sha256(item),
            executed=item.get("error") is None,
            knowledge_source_ids=(
                context.knowledge_source_id,
            ),
            returned_reference_count=verified_references_by_activity.get(
                activity_key,
                0,
            ),
        )
        for item, activity_key in zip(
            search_activities,
            search_activity_keys,
            strict=True,
        )
    )
    warnings = tuple(sorted({
        *_warning_codes(accounting.warning_codes),
        *((degradation_code,) if degradation_code is not None else ()),
        *(
            str(item)
            for activity_item in activity_raw
            for item in _warning_codes(
                activity_item.get("warning") or activity_item.get("warnings")
            )
        ),
    }))
    source_failure_ids = tuple(sorted(
        item.source_call_id for item in source_calls if item.status == "failed"
    ))
    missing_ids = tuple(sorted(set(required_ids) - set(returned_ids)))
    output_truncated = (
        accounting.truncated
        or bool(response.get("truncated", False))
        or any(item.truncated for item in activity)
    )
    adjacency_gap = (
        retrieval_scope.adjacency_hash is not None
        and not response.get("returnedAdjacencyEdges")
    )
    token_accounting_missing = (
        context.retrieval_mode.startswith("agentic_")
        and accounting.output_tokens is None
    )
    role_gap = not set(context.required_role_ids) <= set(returned_roles)
    group_gap = retrieval_scope.group_membership_hash != returned_group_hash
    sequence_gap = retrieval_scope.sequence_hash != returned_sequence_hash
    type_gap = (
        set(context.exact_type_ids) != set(returned_exact_type_ids)
        or set(context.ancestor_type_ids) != set(returned_ancestor_type_ids)
    )
    returned_count = len(returned_ids)
    collection_policy = ontology_scope.collection_policy
    cardinality_gap = (
        (
            collection_policy.expected_cardinality is not None
            and returned_count != collection_policy.expected_cardinality
        )
        or (
            collection_policy.minimum_cardinality is not None
            and returned_count < collection_policy.minimum_cardinality
        )
        or (
            collection_policy.maximum_cardinality is not None
            and returned_count > collection_policy.maximum_cardinality
        )
        or (
            collection_policy.required_unique_member_count is not None
            and returned_count != collection_policy.required_unique_member_count
        )
    )
    partial = bool(
        missing_ids
        or unexpected_ids
        or duplicate_ids
        or missing_reference_ids
        or warnings
        or source_failure_ids
        or output_truncated
        or adjacency_gap
        or degradation_code
        or token_accounting_missing
        or role_gap
        or group_gap
        or sequence_gap
        or type_gap
        or cardinality_gap
    )
    failures: list[RetrievalFailure] = []
    if missing_ids:
        failures.append(RetrievalFailure(
            reason_code="required_member_missing",
            remediation="downstream_abstention_required",
            canonical_ids=missing_ids,
        ))
    if unexpected_ids:
        failures.append(RetrievalFailure(
            reason_code="unexpected_member",
            remediation="operator_repair_required",
            canonical_ids=unexpected_ids,
        ))
    if duplicate_ids:
        failures.append(RetrievalFailure(
            reason_code="duplicate_member",
            remediation="operator_repair_required",
            canonical_ids=duplicate_ids,
        ))
    if missing_reference_ids:
        failures.append(RetrievalFailure(
            reason_code="reference_missing",
            remediation="downstream_abstention_required",
        ))
    for reason_code, offending_ids in sorted(quarantined_findings.items()):
        remediation = (
            "operator_repair_required"
            if reason_code in {
                "citation_invalid",
                "citation_unauthorized",
                "collection_hash_mismatch",
                "scope_key_missing",
                "unknown_relationship",
            }
            else "downstream_abstention_required"
        )
        failures.append(RetrievalFailure(
            reason_code=reason_code,
            remediation=remediation,
            canonical_ids=tuple(sorted(offending_ids)),
        ))
    if warnings or source_failure_ids:
        failures.append(RetrievalFailure(
            reason_code="source_failure",
            remediation="retry_same_scope",
        ))
    if output_truncated:
        failures.append(RetrievalFailure(
            reason_code="output_truncated",
            remediation="new_scope_required",
        ))
    if adjacency_gap:
        failures.append(RetrievalFailure(
            reason_code="adjacency_mismatch",
            remediation="downstream_abstention_required",
        ))
    if degradation_code is not None:
        failures.append(RetrievalFailure(
            reason_code="capability_unavailable",
            remediation="retry_same_scope",
        ))
    if token_accounting_missing:
        failures.append(RetrievalFailure(
            reason_code="activity_missing",
            remediation="downstream_abstention_required",
        ))
    if role_gap:
        failures.append(RetrievalFailure(
            reason_code="required_role_missing",
            remediation="downstream_abstention_required",
        ))
    if group_gap:
        failures.append(RetrievalFailure(
            reason_code="group_mismatch",
            remediation="downstream_abstention_required",
        ))
    if sequence_gap:
        failures.append(RetrievalFailure(
            reason_code="sequence_mismatch",
            remediation="downstream_abstention_required",
        ))
    if type_gap:
        failures.append(RetrievalFailure(
            reason_code="hierarchy_scope_mismatch",
            remediation="downstream_abstention_required",
        ))
    if cardinality_gap:
        failures.append(RetrievalFailure(
            reason_code="cardinality_mismatch",
            remediation="downstream_abstention_required",
        ))
    if not failures and not required_ids:
        failures = []
    runtime_ms = accounting.latency_ms + accounting.retry_wait_ms
    output_tokens = accounting.output_tokens or 0
    output_bytes = len(canonical_json(response).encode("utf-8"))
    agentic_mode = context.retrieval_mode.startswith("agentic_")
    direct_mode = context.retrieval_mode == "direct_hybrid_prefilter"
    observed_search_records = verified_document_count
    observed_graph_requests = 1
    observed_graph_records = len(set(ontology_scope.included_canonical_ids))
    observed_agentic_invocations = 1 if agentic_mode else 0
    observed_agentic_subqueries = len(planned) if agentic_mode else 0
    observed_agentic_source_calls = len(source_calls) if agentic_mode else 0
    observed_direct_requests = len(source_calls) if direct_mode else 0
    observed_vector_requests = (
        accounting.vector_search_requests if direct_mode else 0
    )
    observed_embedding_calls = accounting.embedding_calls if direct_mode else 0
    observed_embedding_items = accounting.embedding_items if direct_mode else 0
    observed_retry_count = accounting.retry_count
    observed_retry_wait = accounting.retry_wait_ms
    observations = (
        (
            "max_ontology_graph_scope_requests",
            observed_graph_requests,
            budget.max_ontology_graph_scope_requests,
        ),
        (
            "max_agentic_retrieval_invocations",
            observed_agentic_invocations,
            budget.max_agentic_retrieval_invocations,
        ),
        (
            "max_agentic_internal_subqueries",
            observed_agentic_subqueries,
            budget.max_agentic_internal_subqueries,
        ),
        (
            "max_agentic_source_calls",
            observed_agentic_source_calls,
            budget.max_agentic_source_calls,
        ),
        (
            "max_direct_search_requests",
            observed_direct_requests,
            budget.max_direct_search_requests,
        ),
        ("max_output_documents", observed_search_records, budget.max_output_documents),
        ("max_output_tokens", output_tokens, budget.max_output_tokens),
        ("max_output_bytes", output_bytes, budget.max_output_bytes),
        ("max_runtime_milliseconds", runtime_ms, budget.max_runtime_milliseconds),
        (
            "max_graph_result_records",
            observed_graph_records,
            budget.max_graph_result_records,
        ),
        (
            "max_search_result_records",
            observed_search_records,
            budget.max_search_result_records,
        ),
        (
            "max_search_candidate_records",
            candidate_observation,
            budget.max_search_candidate_records,
        ),
        (
            "max_vector_search_requests",
            observed_vector_requests,
            budget.max_vector_search_requests,
        ),
        (
            "max_embedding_calls",
            observed_embedding_calls,
            budget.max_embedding_calls,
        ),
        (
            "max_embedding_items",
            observed_embedding_items,
            budget.max_embedding_items,
        ),
        ("max_retry_count", observed_retry_count, budget.max_retry_count),
        (
            "max_retry_wait_milliseconds",
            observed_retry_wait,
            budget.max_retry_wait_milliseconds,
        ),
    )
    exhausted = tuple(sorted(
        name for name, observed, ceiling in observations if observed > ceiling
    ))
    if exhausted:
        if not any(
            failure.reason_code == "retrieval_budget_exhausted"
            for failure in failures
        ):
            failures.append(RetrievalFailure(
                reason_code="retrieval_budget_exhausted",
                remediation="downstream_abstention_required",
            ))
        partial = True
    budget_observation = CoverageBudgetObservationV1_1(
        max_ontology_graph_scope_requests=budget.max_ontology_graph_scope_requests,
        max_agentic_retrieval_invocations=budget.max_agentic_retrieval_invocations,
        max_agentic_internal_subqueries=budget.max_agentic_internal_subqueries,
        max_agentic_source_calls=budget.max_agentic_source_calls,
        max_direct_search_requests=budget.max_direct_search_requests,
        max_output_documents=budget.max_output_documents,
        max_output_tokens=budget.max_output_tokens,
        max_output_bytes=budget.max_output_bytes,
        max_runtime_milliseconds=budget.max_runtime_milliseconds,
        max_graph_result_records=budget.max_graph_result_records,
        max_search_result_records=budget.max_search_result_records,
        max_search_candidate_records=budget.max_search_candidate_records,
        max_vector_search_requests=budget.max_vector_search_requests,
        max_embedding_calls=budget.max_embedding_calls,
        max_embedding_items=budget.max_embedding_items,
        max_retry_count=budget.max_retry_count,
        max_retry_wait_milliseconds=budget.max_retry_wait_milliseconds,
        observed_ontology_graph_scope_requests=observed_graph_requests,
        observed_agentic_retrieval_invocations=observed_agentic_invocations,
        observed_agentic_internal_subqueries=observed_agentic_subqueries,
        observed_agentic_source_calls=observed_agentic_source_calls,
        observed_direct_search_requests=observed_direct_requests,
        observed_output_documents=observed_search_records,
        observed_output_tokens=output_tokens,
        observed_output_bytes=output_bytes,
        observed_runtime_milliseconds=runtime_ms,
        observed_graph_result_records=observed_graph_records,
        observed_search_result_records=observed_search_records,
        observed_search_candidate_records=candidate_observation,
        observed_vector_search_requests=observed_vector_requests,
        observed_embedding_calls=observed_embedding_calls,
        observed_embedding_items=observed_embedding_items,
        observed_retry_count=observed_retry_count,
        observed_retry_wait_milliseconds=observed_retry_wait,
        budget_exhausted_dimensions=exhausted,
    )
    returned_collection_hash = canonical_sha256(
        [item.model_dump(mode="json") for item in returned_member_tuple]
    )
    coverage_values = {
        "identity": AgenticRetrievalCoverageReceiptIdentityV1_1.model_validate(
            {
                **context.identity.model_dump(mode="python", round_trip=True),
                "contract_kind": "c0.agentic_retrieval_coverage_receipt",
            }
        ),
        "coverage_receipt_id": deterministic_contract_id(
            "agentic-retrieval-coverage",
            {
                "context": context.request_context_hash,
                "response": canonical_sha256(response),
            },
        ),
        "request_context_id": context.request_context_id,
        "request_context_hash": context.request_context_hash,
        "resolved_retrieval_scope_id": retrieval_scope.resolved_retrieval_scope_id,
        "resolved_retrieval_scope_hash": retrieval_scope.retrieval_scope_hash,
        "provider_request_id": _opaque_external_id(
            "provider-request",
            response.get("requestId")
            if response.get("requestId") is not None
            else accounting.operation_refs[0],
        ),
        "provider_correlation_id": (
            _opaque_external_id(
                "provider-correlation",
                response.get("correlationId"),
            )
            if response.get("correlationId") is not None
            else None
        ),
        "retrieval_mode": context.retrieval_mode,
        "api_version": context.capability.api_version,
        "capability_fingerprint": context.capability.capability_fingerprint,
        "fallback_used": originating_context is not None,
        "fallback_reason_code": fallback_reason_code,
        "planned_subqueries": planned,
        "activity": activity,
        "source_calls": source_calls,
        "matched_document_count": candidate_observation,
        "returned_document_count": observed_search_records,
        "reference_count": verified_document_count,
        "unique_canonical_id_count": len(returned_ids),
        "canonical_citation_count": len(citation_mappings),
        "returned_members": returned_member_tuple,
        "returned_adjacency_edges": (),
        "required_canonical_ids": required_ids,
        "returned_canonical_ids": returned_ids,
        "missing_canonical_ids": missing_ids,
        "unexpected_canonical_ids": unexpected_ids,
        "duplicate_canonical_ids": duplicate_ids,
        "orphan_canonical_ids": tuple(sorted({
            *unexpected_ids,
            *quarantined_unexpected_ids,
        })),
        "required_canonical_id_set_hash": canonical_sha256(sorted(required_ids)),
        "returned_canonical_id_set_hash": canonical_sha256(sorted(returned_ids)),
        "required_group_hash": retrieval_scope.group_membership_hash,
        "returned_group_hash": returned_group_hash,
        "required_sequence_hash": retrieval_scope.sequence_hash,
        "returned_sequence_hash": returned_sequence_hash,
        "required_adjacency_hash": retrieval_scope.adjacency_hash,
        "returned_adjacency_hash": None,
        "required_role_ids": retrieval_scope.required_role_ids,
        "returned_role_ids": returned_roles,
        "expected_cardinality": ontology_scope.collection_policy.expected_cardinality,
        "minimum_cardinality": ontology_scope.collection_policy.minimum_cardinality,
        "maximum_cardinality": ontology_scope.collection_policy.maximum_cardinality,
        "required_unique_member_count": (
            ontology_scope.collection_policy.required_unique_member_count
        ),
        "returned_unique_member_count": len(returned_ids),
        "required_collection_hash": retrieval_scope.collection_hash,
        "returned_collection_hash": returned_collection_hash,
        "requested_exact_type_ids": context.exact_type_ids,
        "returned_exact_type_ids": returned_exact_type_ids,
        "requested_ancestor_type_ids": context.ancestor_type_ids,
        "returned_ancestor_type_ids": returned_ancestor_type_ids,
        "type_hierarchy_hash": context.type_hierarchy_hash,
        "hierarchy_scope_mode": context.hierarchy_scope_mode,
        "type_assertion_set_hash": context.type_assertion_set_hash,
        "citation_mappings": tuple(citation_mappings),
        "missing_reference_ids": tuple(sorted(set(missing_reference_ids))),
        "warning_codes": warnings,
        "source_failure_ids": source_failure_ids,
        "output_truncated": output_truncated,
        "partial_response": partial,
        "unsupported_capability_codes": tuple(sorted({
            *(
                (degradation_code,)
                if degradation_code is not None
                else ()
            ),
            *(
                ("output_token_accounting_missing",)
                if token_accounting_missing
                else ()
            ),
        })),
        "budget": budget_observation,
        "retrieval_reasoning_effort": context.retrieval_reasoning_effort,
        "coverage_semantics": "bounded_maximal",
        "coverage_status": (
            "abstain" if exhausted and not citations else
            "partial" if partial else
            "complete"
        ),
        "failures": tuple(failures),
    }
    coverage = AgenticRetrievalCoverageReceiptV1_1(
        **coverage_values,
        coverage_receipt_hash=canonical_sha256(coverage_values),
    )
    coverage.validate_request_context(
        context,
        budget,
        originating_context=originating_context,
        originating_budget=originating_budget,
    )
    coverage.validate_citations(tuple(citations))
    return L5bRetrievalResult(
        citations=tuple(citations),
        presentations=tuple(presentations),
        coverage=coverage,
    )
