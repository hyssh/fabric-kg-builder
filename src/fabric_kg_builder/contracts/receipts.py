"""Artifact manifests and immutable stage receipts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from .base import (
    ContractModel,
    RequiredText,
    SemVer,
    Sha256,
    canonical_sha256,
    frozen_mapping,
    reject_secret_text,
    sorted_unique,
    utc_timestamp,
)
from .identity import CanonicalIdentityEnvelope, ImmutableSourceLocator

NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
StageId = Literal["C0", "L1", "L2", "L3", "L4", "L5", "L6", "L7"]


class ArtifactEntry(ContractModel):
    artifact_id: RequiredText
    contract_kind: RequiredText
    contract_version: SemVer
    schema_hash: Sha256
    content_hash: Sha256
    canonical_id_set_hash: Sha256 | None
    row_count: NonNegativeInt | None
    byte_count: NonNegativeInt
    partition_count: NonNegativeInt
    media_type: RequiredText
    immutable_locator: ImmutableSourceLocator | None
    blob_asset_ref_id: RequiredText | None


class ArtifactManifest(ContractModel):
    identity: CanonicalIdentityEnvelope
    artifact_manifest_id: RequiredText
    entries: tuple[ArtifactEntry, ...]
    total_row_count: NonNegativeInt
    total_byte_count: NonNegativeInt
    manifest_hash: Sha256

    @field_validator("entries", mode="before")
    @classmethod
    def _entries(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.artifact_id
                        if isinstance(item, ArtifactEntry)
                        else str(item.get("artifact_id", ""))
                    ),
                )
            )
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "ArtifactManifest":
        if self.identity.contract_kind != "c0.artifact_manifest":
            raise ValueError("invalid artifact manifest identity contract_kind")
        ids = [entry.artifact_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("artifact IDs must be unique")
        known_rows = sum(
            entry.row_count for entry in self.entries if entry.row_count is not None
        )
        if self.total_row_count != known_rows:
            raise ValueError("total_row_count must equal declared entry row counts")
        if self.total_byte_count != sum(entry.byte_count for entry in self.entries):
            raise ValueError("total_byte_count must equal entry byte counts")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"manifest_hash"})
        )
        if self.manifest_hash != expected:
            raise ValueError("manifest_hash does not match artifact manifest")
        return self


class StageReceipt(ContractModel):
    identity: CanonicalIdentityEnvelope
    stage_receipt_id: RequiredText
    stage_id: StageId
    stage_name: RequiredText
    stage_contract_version: SemVer
    status: Literal["succeeded", "failed", "skipped", "blocked"]
    input_manifest_id: RequiredText
    input_manifest_hash: Sha256
    output_manifest_id: RequiredText | None
    output_manifest_hash: Sha256 | None
    skip_key: Sha256
    accepted_contract_versions: Mapping[str, RequiredText]
    resource_metrics_id: RequiredText
    resource_metrics_hash: Sha256
    attempt_count: PositiveInt
    remote_operation_refs: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()
    started_at_utc: datetime
    completed_at_utc: datetime
    receipt_hash: Sha256

    @field_validator("started_at_utc", "completed_at_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return utc_timestamp(value)

    @field_validator("remote_operation_refs", "error_codes", mode="before")
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            normalized = sorted_unique(value, field_name=info.field_name)
            for item in normalized:
                reject_secret_text(item, field_name=info.field_name)
                parsed = urlparse(item)
                if parsed.username is not None or parsed.password is not None:
                    raise ValueError(
                        f"{info.field_name} must not contain URI credentials"
                    )
            return normalized
        return value

    @field_validator("accepted_contract_versions", mode="after")
    @classmethod
    def _freeze_versions(
        cls,
        value: Mapping[str, str],
    ) -> Mapping[str, str]:
        return frozen_mapping(value)

    @model_validator(mode="after")
    def _invariants(self) -> "StageReceipt":
        if self.identity.contract_kind != "c0.stage_receipt":
            raise ValueError("invalid stage receipt identity contract_kind")
        if (self.output_manifest_id is None) != (self.output_manifest_hash is None):
            raise ValueError("output manifest ID and hash must be paired")
        if self.status in {"succeeded", "skipped"} and self.output_manifest_id is None:
            raise ValueError("successful or skipped receipts require an output manifest")
        if self.status in {"failed", "blocked"} and not self.error_codes:
            raise ValueError("failed or blocked receipts require error codes")
        if self.status in {"succeeded", "skipped"} and self.error_codes:
            raise ValueError("successful or skipped receipts cannot contain error codes")
        if self.completed_at_utc < self.started_at_utc:
            raise ValueError("completed_at_utc must not precede started_at_utc")
        expected = canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"receipt_hash", "started_at_utc", "completed_at_utc"},
            )
        )
        if self.receipt_hash != expected:
            raise ValueError("receipt_hash does not match stage receipt")
        return self


def validate_skip_preconditions(
    candidate: StageReceipt,
    *,
    prior_succeeded: StageReceipt,
    intact_output_manifest: ArtifactManifest,
) -> None:
    """Fail closed unless a skipped stage reuses an intact succeeded receipt."""
    if candidate.status != "skipped":
        raise ValueError("candidate receipt is not a skip")
    if prior_succeeded.status != "succeeded":
        raise ValueError("only a succeeded receipt may authorize a skip")
    if candidate.skip_key != prior_succeeded.skip_key:
        raise ValueError("skip key does not match the prior succeeded receipt")
    if candidate.input_manifest_hash != prior_succeeded.input_manifest_hash:
        raise ValueError("skip input manifest hash changed")
    if candidate.output_manifest_id != prior_succeeded.output_manifest_id:
        raise ValueError("skip output manifest ID changed")
    if candidate.output_manifest_hash != prior_succeeded.output_manifest_hash:
        raise ValueError("skip output manifest hash changed")
    if candidate.output_manifest_id != intact_output_manifest.artifact_manifest_id:
        raise ValueError("skip output artifact manifest is missing or replaced")
    if candidate.output_manifest_hash != intact_output_manifest.manifest_hash:
        raise ValueError("skip output artifacts are not intact")
