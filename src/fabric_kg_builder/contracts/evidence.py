"""Immutable source-unit and verifier-minted evidence-span contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from .base import (
    ContractModel,
    RequiredText,
    Sha256,
    canonical_sha256,
    deterministic_contract_id,
    normalize_nfc,
    utf8_sha256,
    utc_timestamp,
)
from .identity import CanonicalIdentityEnvelope, ImmutableSourceLocator

NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]


class SourceUnit(ContractModel):
    identity: CanonicalIdentityEnvelope
    source_unit_id: RequiredText
    source_file_id: RequiredText
    unit_kind: Literal[
        "heading", "paragraph", "table", "cell", "visual_description", "transcript"
    ]
    text: str
    text_content_hash: Sha256
    encoding: Literal["utf-8"] = "utf-8"
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"
    ordinal: NonNegativeInt
    parent_source_unit_id: RequiredText | None = None
    locator: ImmutableSourceLocator
    byte_count: NonNegativeInt
    codepoint_count: NonNegativeInt

    @model_validator(mode="after")
    def _invariants(self) -> "SourceUnit":
        if self.identity.contract_kind != "c0.source_unit":
            raise ValueError("SourceUnit identity contract_kind must be c0.source_unit")
        if self.identity.source_unit_id != self.source_unit_id:
            raise ValueError("source_unit_id must equal identity.source_unit_id")
        if self.identity.source_file_id != self.source_file_id:
            raise ValueError("source_file_id must equal identity.source_file_id")
        if self.identity.immutable_locator != self.locator:
            raise ValueError("locator must equal identity.immutable_locator")
        expected_id = deterministic_contract_id(
            "source-unit",
            {
                "asset_version_id": self.identity.asset_version_id,
                "source_file_id": self.source_file_id,
                "locator_hash": self.locator.locator_hash,
                "ordinal": self.ordinal,
            },
        )
        if self.source_unit_id != expected_id:
            raise ValueError("source_unit_id does not match its deterministic seed")
        if self.text_content_hash != utf8_sha256(self.text):
            raise ValueError("text_content_hash must hash exact NFC UTF-8 text")
        if self.codepoint_count != len(self.text):
            raise ValueError("codepoint_count must equal len(text)")
        if self.byte_count != len(self.text.encode("utf-8")):
            raise ValueError("byte_count must equal exact UTF-8 byte length")
        return self

    @classmethod
    def mint(
        cls,
        *,
        identity: CanonicalIdentityEnvelope,
        unit_kind: str,
        text: str,
        ordinal: int,
        locator: ImmutableSourceLocator,
        parent_source_unit_id: str | None = None,
    ) -> "SourceUnit":
        text = normalize_nfc(text)
        source_unit_id = deterministic_contract_id(
            "source-unit",
            {
                "asset_version_id": identity.asset_version_id,
                "source_file_id": identity.source_file_id,
                "locator_hash": locator.locator_hash,
                "ordinal": ordinal,
            },
        )
        updated_identity = identity.model_copy(
            update={
                "contract_kind": "c0.source_unit",
                "source_unit_id": source_unit_id,
                "immutable_locator": locator,
            }
        )
        return cls(
            identity=updated_identity,
            source_unit_id=source_unit_id,
            source_file_id=updated_identity.source_file_id,
            unit_kind=unit_kind,
            text=text,
            text_content_hash=utf8_sha256(text),
            ordinal=ordinal,
            parent_source_unit_id=parent_source_unit_id,
            locator=locator,
            byte_count=len(text.encode("utf-8")),
            codepoint_count=len(text),
        )


class EvidenceSpan(ContractModel):
    identity: CanonicalIdentityEnvelope
    evidence_span_id: RequiredText
    source_unit_id: RequiredText
    source_file_id: RequiredText
    asset_version_id: RequiredText
    span_start: NonNegativeInt
    span_end: PositiveInt
    quote: Annotated[str, Field(min_length=1)]
    quote_hash: Sha256
    source_text_content_hash: Sha256
    locator: ImmutableSourceLocator
    verification_status: Literal["verified"] = "verified"
    verifier_name: RequiredText
    verifier_version: RequiredText
    verified_at_utc: datetime

    _utc = field_validator("verified_at_utc")(utc_timestamp)

    @model_validator(mode="after")
    def _identity_invariants(self) -> "EvidenceSpan":
        if self.identity.contract_kind != "c0.evidence_span":
            raise ValueError("EvidenceSpan identity contract_kind must be c0.evidence_span")
        if self.identity.source_unit_id != self.source_unit_id:
            raise ValueError("source_unit_id must equal identity.source_unit_id")
        if self.identity.source_file_id != self.source_file_id:
            raise ValueError("source_file_id must equal identity.source_file_id")
        if self.identity.asset_version_id != self.asset_version_id:
            raise ValueError("asset_version_id must equal identity.asset_version_id")
        if self.identity.immutable_locator != self.locator:
            raise ValueError("locator must equal identity.immutable_locator")
        if self.span_end <= self.span_start:
            raise ValueError("span_end must be greater than span_start")
        if (
            self.locator.char_start != self.span_start
            or self.locator.char_end != self.span_end
        ):
            raise ValueError("evidence locator offsets must equal the span offsets")
        if self.quote_hash != utf8_sha256(self.quote):
            raise ValueError("quote_hash must hash the exact quote")
        expected_id = deterministic_contract_id(
            "evidence-span",
            {
                "source_unit_id": self.source_unit_id,
                "span_start": self.span_start,
                "span_end": self.span_end,
                "quote_hash": self.quote_hash,
                "verifier_name": self.verifier_name,
                "verifier_version": self.verifier_version,
            },
        )
        if self.evidence_span_id != expected_id:
            raise ValueError("evidence_span_id was not minted from verified span fields")
        return self

    @classmethod
    def mint_verified(
        cls,
        *,
        source_unit: SourceUnit,
        span_start: int,
        span_end: int,
        verifier_name: str,
        verifier_version: str,
        verified_at_utc: datetime,
    ) -> "EvidenceSpan":
        """Verify exact code-point offsets before minting the evidence ID."""
        if not 0 <= span_start < span_end <= source_unit.codepoint_count:
            raise ValueError("evidence span is outside the exact SourceUnit text")
        quote = source_unit.text[span_start:span_end]
        if not quote:
            raise ValueError("evidence quote must not be empty")
        locator_raw: dict[str, Any] = source_unit.locator.to_authority()
        locator_raw["char_start"] = span_start
        locator_raw["char_end"] = span_end
        locator_raw["locator_version"] = "1.0"
        locator_raw["locator_hash"] = canonical_sha256(locator_raw)
        locator = ImmutableSourceLocator.model_validate(locator_raw)
        evidence_span_id = deterministic_contract_id(
            "evidence-span",
            {
                "source_unit_id": source_unit.source_unit_id,
                "span_start": span_start,
                "span_end": span_end,
                "quote_hash": utf8_sha256(quote),
                "verifier_name": verifier_name,
                "verifier_version": verifier_version,
            },
        )
        identity = source_unit.identity.model_copy(
            update={
                "contract_kind": "c0.evidence_span",
                "immutable_locator": locator,
            }
        )
        return cls(
            identity=identity,
            evidence_span_id=evidence_span_id,
            source_unit_id=source_unit.source_unit_id,
            source_file_id=source_unit.source_file_id,
            asset_version_id=source_unit.identity.asset_version_id,
            span_start=span_start,
            span_end=span_end,
            quote=quote,
            quote_hash=utf8_sha256(quote),
            source_text_content_hash=source_unit.text_content_hash,
            locator=locator,
            verifier_name=verifier_name,
            verifier_version=verifier_version,
            verified_at_utc=verified_at_utc,
        )

    def verify_against(self, source_unit: SourceUnit) -> None:
        if source_unit.source_unit_id != self.source_unit_id:
            raise ValueError("evidence references a different SourceUnit")
        if source_unit.source_file_id != self.source_file_id:
            raise ValueError("evidence source_file_id mismatch")
        if source_unit.identity.asset_version_id != self.asset_version_id:
            raise ValueError("evidence asset_version_id mismatch")
        if source_unit.text_content_hash != self.source_text_content_hash:
            raise ValueError("evidence source text hash mismatch")
        if self.span_end > source_unit.codepoint_count:
            raise ValueError("evidence span exceeds SourceUnit code-point range")
        if source_unit.text[self.span_start:self.span_end] != self.quote:
            raise ValueError("evidence quote does not equal the exact source substring")
        if self.locator.char_start != self.span_start or self.locator.char_end != self.span_end:
            raise ValueError("evidence locator offsets are inconsistent")
        unit_locator = source_unit.locator.to_authority()
        evidence_locator = self.locator.to_authority()
        for field_name in ("char_start", "char_end"):
            unit_locator.pop(field_name, None)
            evidence_locator.pop(field_name, None)
        if evidence_locator != unit_locator:
            raise ValueError(
                "evidence locator source coordinates differ from SourceUnit locator"
            )
