"""Shared local SourceUnit/EvidenceSpan verifier with purpose isolation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fabric_kg_builder.contracts.base import canonical_sha256, normalize_nfc
from fabric_kg_builder.contracts.evidence import EvidenceSpan, SourceUnit
from fabric_kg_builder.contracts.identity import (
    CanonicalIdentityEnvelope,
    ImmutableSourceLocator,
)

from .corpus import SourceCorpusEntry

EvidencePurpose = Literal["domain_design", "extraction", "validation"]
LOCAL_VERIFIER_VERSION = "1.0.0"
LOCAL_VERIFIER_PREFIX = "fabric-kg.local-evidence-verifier"


def verifier_name(purpose: EvidencePurpose) -> str:
    return f"{LOCAL_VERIFIER_PREFIX}/{purpose}"


def design_locator(
    *,
    source_corpus_manifest_id: str,
    source_file_id: str,
    section_path: tuple[str, ...] | None = None,
    page: int | None = None,
    sheet: str | None = None,
    slide: int | None = None,
    cell_range: str | None = None,
) -> ImmutableSourceLocator:
    """Create a secret-free immutable locator for a local corpus entry."""
    values = {
        "locator_version": "1.0",
        "blob_uri": None,
        "blob_version_id": None,
        "source_uri": (
            "https://fabric-kg.local/corpus/"
            f"{source_corpus_manifest_id}/source/{source_file_id}"
        ),
        "page": page,
        "sheet": sheet,
        "slide": slide,
        "section_path": section_path,
        "cell_range": cell_range,
        "char_start": None,
        "char_end": None,
        "polygon": None,
        "sheet_zone": None,
        "tile_id": None,
        "coordinate_system": None,
        "transform": None,
        "native_layer_id": None,
        "native_object_id": None,
    }
    return ImmutableSourceLocator(
        **values,
        locator_hash=canonical_sha256(values),
    )


def mint_source_unit(
    *,
    base_identity: CanonicalIdentityEnvelope,
    corpus_entry: SourceCorpusEntry,
    source_corpus_manifest_id: str,
    unit_kind: Literal[
        "heading", "paragraph", "table", "cell", "visual_description", "transcript"
    ],
    text: str,
    ordinal: int,
    section_path: tuple[str, ...] | None = None,
    page: int | None = None,
    sheet: str | None = None,
    slide: int | None = None,
    cell_range: str | None = None,
) -> SourceUnit:
    """Mint one exact NFC design/extraction SourceUnit from adapter output."""
    locator = design_locator(
        source_corpus_manifest_id=source_corpus_manifest_id,
        source_file_id=corpus_entry.source_file_id,
        section_path=section_path,
        page=page,
        sheet=sheet,
        slide=slide,
        cell_range=cell_range,
    )
    identity = base_identity.model_copy(
        update={
            "contract_kind": "c0.source_unit",
            "asset_id": corpus_entry.asset_id,
            "asset_version_id": corpus_entry.asset_version_id,
            "source_file_id": corpus_entry.source_file_id,
            "source_unit_id": None,
            "content_hash": corpus_entry.original_byte_hash,
            "extractor_name": "fabric-kg.design-sample-adapter",
            "extractor_version": "1.0.0",
            "immutable_locator": locator,
            "parent_artifact_ids": (source_corpus_manifest_id,),
        }
    )
    return SourceUnit.mint(
        identity=identity,
        unit_kind=unit_kind,
        text=normalize_nfc(text),
        ordinal=ordinal,
        locator=locator,
    )


def mint_verified_span(
    *,
    source_unit: SourceUnit,
    span_start: int,
    span_end: int,
    purpose: EvidencePurpose,
    verified_at_utc: datetime,
    expected_quote: str | None = None,
) -> EvidenceSpan:
    """Verify Unicode-codepoint offsets locally before C0 mints the evidence ID."""
    if expected_quote is not None:
        normalized_quote = normalize_nfc(expected_quote)
        if source_unit.text[span_start:span_end] != normalized_quote:
            raise ValueError("expected quote does not equal the exact source substring")
    span = EvidenceSpan.mint_verified(
        source_unit=source_unit,
        span_start=span_start,
        span_end=span_end,
        verifier_name=verifier_name(purpose),
        verifier_version=LOCAL_VERIFIER_VERSION,
        verified_at_utc=verified_at_utc,
    )
    span.verify_against(source_unit)
    return span


def verify_span_for_purpose(
    span: EvidenceSpan,
    source_unit: SourceUnit,
    *,
    purpose: EvidencePurpose,
) -> None:
    """Reject valid spans minted for a different layer purpose."""
    if span.verifier_name != verifier_name(purpose):
        raise ValueError(
            f"evidence purpose mismatch: expected {purpose}, got {span.verifier_name}"
        )
    if span.verifier_version != LOCAL_VERIFIER_VERSION:
        raise ValueError("evidence verifier version mismatch")
    span.verify_against(source_unit)
