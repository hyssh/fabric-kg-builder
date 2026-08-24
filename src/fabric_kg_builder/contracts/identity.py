"""Canonical cross-layer identity and immutable source locator contracts."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import parse_qsl, urlparse

from pydantic import (
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from .base import (
    CONTRACT_VERSION,
    ContractModel,
    RequiredText,
    Sha256,
    canonical_sha256,
    freeze_json,
    reject_secret_text,
    sorted_unique,
    thaw_json,
)

if TYPE_CHECKING:
    from fabric_kg_builder.model.schemas import CommonLineageRow

NonNegativeInt = Annotated[int, Field(ge=0)]


class ImmutableSourceLocator(ContractModel):
    """Typed immutable adapter over ``build_source_locator``."""

    locator_version: Literal["1.0"] = "1.0"
    blob_uri: str | None = None
    blob_version_id: str | None = None
    source_uri: str | None = None
    page: NonNegativeInt | None = None
    sheet: str | None = None
    slide: NonNegativeInt | None = None
    section_path: tuple[str, ...] | None = None
    cell_range: str | None = None
    char_start: NonNegativeInt | None = None
    char_end: NonNegativeInt | None = None
    polygon: JsonValue | None = None
    sheet_zone: str | None = None
    tile_id: str | None = None
    coordinate_system: str | None = None
    transform: JsonValue | None = None
    native_layer_id: str | None = None
    native_object_id: str | None = None
    locator_hash: Sha256

    @field_validator("polygon", "transform", mode="after")
    @classmethod
    def _freeze_coordinates(cls, value: JsonValue | None) -> JsonValue | None:
        return freeze_json(value)

    @field_serializer("polygon", "transform")
    def _serialize_coordinates(self, value: JsonValue | None) -> JsonValue | None:
        return thaw_json(value)

    @field_validator("section_path", mode="before")
    @classmethod
    def _section_path(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part for part in value.split("/") if part)
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("blob_uri", "source_uri")
    @classmethod
    def _immutable_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        reject_secret_text(value, field_name="source locator URI")
        parsed = urlparse(value)
        if re.match(r"^[A-Za-z]:[\\/]", value):
            raise ValueError("Windows local paths are forbidden in immutable locators")
        azure_filesystem_schemes = {"abfs", "abfss", "wasb", "wasbs"}
        if parsed.password is not None or (
            parsed.username is not None
            and parsed.scheme.lower() not in azure_filesystem_schemes
        ):
            raise ValueError("credential-bearing URI user-info is forbidden")
        allowed_schemes = {"https", "abfs", "abfss", "wasb", "wasbs"}
        if parsed.scheme.lower() not in allowed_schemes:
            raise ValueError("mutable local paths are forbidden in immutable locators")
        forbidden_keys = {
            "sig", "se", "sp", "sv", "spr", "st", "token", "access_token",
            "client_secret", "accountkey",
        }
        if forbidden_keys.intersection(key.casefold() for key, _ in parse_qsl(parsed.query)):
            raise ValueError("signed or credential-bearing URIs are forbidden")
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "ImmutableSourceLocator":
        coordinates = (
            self.blob_uri,
            self.blob_version_id,
            self.source_uri,
            self.page,
            self.sheet,
            self.slide,
            self.section_path,
            self.cell_range,
            self.char_start,
            self.char_end,
            self.polygon,
            self.sheet_zone,
            self.tile_id,
            self.native_layer_id,
            self.native_object_id,
        )
        if all(value is None for value in coordinates):
            raise ValueError("at least one immutable source coordinate is required")
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must be provided together")
        if self.char_start is not None and self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"locator_hash"})
        )
        if self.locator_hash != expected:
            raise ValueError("locator_hash does not match canonical locator content")
        return self

    @classmethod
    def from_authority(cls, **kwargs: Any) -> "ImmutableSourceLocator":
        """Build from the existing lineage locator vocabulary and seal its hash."""
        from fabric_kg_builder.lineage.common import build_source_locator

        raw = build_source_locator(**kwargs)
        raw["locator_version"] = "1.0"
        raw["locator_hash"] = canonical_sha256(raw)
        return cls.model_validate(raw)

    def to_authority(self) -> dict[str, Any]:
        """Return exactly the fields owned by ``build_source_locator``."""
        return self.model_dump(
            mode="json",
            exclude={"locator_version", "locator_hash"},
        )


class CanonicalIdentityEnvelope(ContractModel):
    """Shared identity references; contract-specific IDs remain authoritative."""

    contract_kind: RequiredText
    contract_version: Literal["1.0.0"] = CONTRACT_VERSION
    project_id: RequiredText
    asset_id: RequiredText | None
    asset_version_id: RequiredText | None
    run_id: RequiredText
    source_file_id: RequiredText | None
    source_unit_id: RequiredText | None
    content_hash: Sha256 | None
    domain_schema_version: Literal["1.0", "2.0"]
    domain_contract_hash: Sha256
    semantic_contract_hash: Sha256 | None
    canonical_schema_version: RequiredText
    prompt_version: RequiredText | None
    prompt_hash: Sha256 | None
    model_version: RequiredText | None
    model_hash: Sha256 | None
    extractor_name: RequiredText | None
    extractor_version: RequiredText | None
    parent_artifact_ids: tuple[str, ...] = ()
    parent_record_ids: tuple[str, ...] = ()
    immutable_locator: ImmutableSourceLocator | None

    @field_validator("parent_artifact_ids", "parent_record_ids", mode="before")
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @model_validator(mode="after")
    def _identity_invariants(self) -> "CanonicalIdentityEnvelope":
        source_values = (
            self.asset_id,
            self.asset_version_id,
            self.source_file_id,
            self.content_hash,
        )
        if any(value is not None for value in source_values) and not all(
            value is not None for value in source_values
        ):
            raise ValueError(
                "source-derived identity requires asset, asset version, source file, "
                "and content hash together"
            )
        for name, version, digest in (
            ("prompt", self.prompt_version, self.prompt_hash),
            ("model", self.model_version, self.model_hash),
        ):
            if (version is None) != (digest is None):
                raise ValueError(f"{name}_version and {name}_hash must be paired")
        if (self.extractor_name is None) != (self.extractor_version is None):
            raise ValueError("extractor_name and extractor_version must be paired")
        if self.source_unit_id is not None and self.source_file_id is None:
            raise ValueError("source_unit_id requires source-derived identity")
        return self

    @classmethod
    def from_common_lineage(
        cls,
        row: CommonLineageRow,
        *,
        contract_kind: str,
        contract_version: str = CONTRACT_VERSION,
        domain_schema_version: Literal["1.0", "2.0"],
        canonical_schema_version: str,
        content_hash: str | None,
        source_file_id: str | None = None,
        source_unit_id: str | None = None,
        semantic_contract_hash: str | None = None,
        parent_artifact_ids: tuple[str, ...] = (),
    ) -> "CanonicalIdentityEnvelope":
        """Adapt existing lineage fields without reinterpreting ``domain_hash``."""
        if not row.domain_hash:
            raise ValueError("CommonLineageRow.domain_hash is required for adaptation")
        locator = None
        if row.source_locator_json:
            import json

            raw = json.loads(row.source_locator_json)
            raw["locator_version"] = "1.0"
            raw["locator_hash"] = canonical_sha256(raw)
            locator = ImmutableSourceLocator.model_validate(raw)
        return cls(
            contract_kind=contract_kind,
            contract_version=contract_version,
            project_id=row.project_id,
            asset_id=row.asset_id or None,
            asset_version_id=row.asset_version_id or None,
            run_id=row.run_id,
            source_file_id=source_file_id,
            source_unit_id=source_unit_id,
            content_hash=content_hash,
            domain_schema_version=domain_schema_version,
            domain_contract_hash=row.domain_hash,
            semantic_contract_hash=semantic_contract_hash,
            canonical_schema_version=canonical_schema_version,
            prompt_version=None,
            prompt_hash=None,
            model_version=None,
            model_hash=None,
            extractor_name=None,
            extractor_version=None,
            parent_artifact_ids=parent_artifact_ids,
            parent_record_ids=(row.parent_record_id,) if row.parent_record_id else (),
            immutable_locator=locator,
        )


class StandaloneCanonicalIdentityEnvelope(CanonicalIdentityEnvelope):
    """Registry artifact form of the otherwise embedded identity envelope."""

    @model_validator(mode="after")
    def _standalone_kind(self) -> "StandaloneCanonicalIdentityEnvelope":
        if self.contract_kind != "c0.identity":
            raise ValueError("standalone identity contract_kind must be c0.identity")
        return self
