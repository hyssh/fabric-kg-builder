from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from fabric_kg_builder.contracts.identity import (
    CanonicalIdentityEnvelope,
    ImmutableSourceLocator,
)
from fabric_kg_builder.enrichment.schema2_sources import (
    CorpusAsset,
    IndexedSourceCorpusReader,
    L2StageError,
    SourceElement,
    materialize_source_corpus,
)
from fabric_kg_builder.domain.stage import make_l1_identity
from fabric_kg_builder.model.schemas import AssetRow, AssetVersionRow
from fabric_kg_builder.sources.corpus import (
    SourceCorpusEntry,
    build_source_corpus_manifest,
)


def _identity() -> CanonicalIdentityEnvelope:
    return make_l1_identity(
        project_id="project:l2-tests",
        run_id="run:l1-tests",
        domain_contract_hash="a" * 64,
    )


@dataclass
class _Reader:
    root: Path
    mutate_bytes: bool = False

    def read(self, entry: SourceCorpusEntry) -> CorpusAsset:
        path = self.root / entry.relative_source_ref
        content = path.read_bytes()
        if self.mutate_bytes:
            content = b"mutated source bytes"
        now = datetime.now(timezone.utc)
        asset = AssetRow(
            asset_id=entry.asset_id,
            project_id="project:l2-tests",
            original_name=path.name,
            media_type=entry.media_type,
            source_uri=f"https://sharepoint.example/{entry.asset_id}",
            created_at=now,
            created_by="test",
        )
        version = AssetVersionRow(
            asset_version_id=entry.asset_version_id,
            asset_id=entry.asset_id,
            version_identity="v1",
            content_hash=entry.original_byte_hash,
            size_bytes=entry.byte_count,
            original_name=path.name,
            media_type=entry.media_type,
            source_uri=asset.source_uri,
            blob_uri=f"https://storage.example/{entry.asset_version_id}",
            blob_version_id="v1",
            landing_path=entry.relative_source_ref,
            registered_at=now,
            landing_timestamp=now,
            ingestion_status="ready",
        )
        locator = ImmutableSourceLocator.from_authority(
            blob_uri=version.blob_uri,
            blob_version_id=version.blob_version_id,
            char_start=0,
            char_end=max(1, len(content)),
        )
        return CorpusAsset(
            asset=asset,
            version=version,
            consumed_byte_hash=(
                hashlib.sha256(content).hexdigest()
                if self.mutate_bytes
                else entry.original_byte_hash
            ),
            consumed_byte_count=len(content),
            adapter_name=entry.adapter_name or "markdown",
            adapter_version="1.0.0",
            elements=(
                SourceElement(
                    element_id=f"element:{entry.source_file_id}",
                    unit_kind="paragraph",
                    text=content.decode("utf-8"),
                    ordinal=0,
                    locator=locator,
                ),
            ),
        )


def _inputs(corpus) -> SimpleNamespace:
    return SimpleNamespace(
        corpus_manifest=corpus,
        l1_receipt=SimpleNamespace(
            identity=_identity(),
            receipt_hash="d" * 64,
        ),
        design_sample_manifest=SimpleNamespace(entries=(corpus.entries[0],)),
    )


def test_full_corpus_is_materialized_not_bounded_design_sample(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("Facility A contains Pump 1.", encoding="utf-8")
    (tmp_path / "b.md").write_text("Facility B contains Pump 2.", encoding="utf-8")
    corpus = build_source_corpus_manifest(
        tmp_path,
        corpus_root_id="corpus:test",
        identity=_identity(),
    )

    result = materialize_source_corpus(
        inputs=_inputs(corpus),
        reader=_Reader(tmp_path),
    )

    assert {unit.source_file_id for unit in result.source_units} == {
        entry.source_file_id for entry in corpus.entries
    }
    assert result.report.source_corpus_entry_count == 2
    assert result.report.materialized_corpus_entry_count == 2
    assert result.report.source_unit_count == 2
    assert all(item.disposition == "materialized" for item in result.report.dispositions)
    assert all(item.source_unit_ids for item in result.report.dispositions)


def test_hash_drift_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "source.md").write_text("Approved source text.", encoding="utf-8")
    corpus = build_source_corpus_manifest(
        tmp_path,
        corpus_root_id="corpus:test",
        identity=_identity(),
    )

    with pytest.raises(L2StageError, match="landed bytes differ"):
        materialize_source_corpus(
            inputs=_inputs(corpus),
            reader=_Reader(tmp_path, mutate_bytes=True),
        )


def test_ineligible_corpus_entries_are_reconciled_without_adapter_calls(
    tmp_path: Path,
) -> None:
    (tmp_path / "unsupported.bin").write_bytes(b"\x00\x01")
    corpus = build_source_corpus_manifest(
        tmp_path,
        corpus_root_id="corpus:test",
        identity=_identity(),
    )

    class _NoRead:
        def read(self, entry):
            raise AssertionError(f"ineligible entry was read: {entry.source_file_id}")

    result = materialize_source_corpus(
        inputs=_inputs(corpus),
        reader=_NoRead(),
    )

    assert result.source_units == ()
    assert result.report.ineligible_corpus_entry_count == 1
    assert result.report.source_unit_count == 0
    assert result.report.dispositions[0].disposition == "excluded"


def test_indexed_reader_uses_immutable_blob_locator_not_mutable_source_uri(
    tmp_path: Path,
) -> None:
    path = tmp_path / "indexed.html"
    path.write_text("<p>immutable bytes</p>", encoding="utf-8")
    content = path.read_bytes()
    corpus = build_source_corpus_manifest(
        path,
        corpus_root_id="corpus:test",
        identity=_identity(),
    )
    entry = corpus.entries[0]
    now = datetime.now(timezone.utc)
    asset = AssetRow(
        asset_id=entry.asset_id,
        project_id="project:l2-tests",
        original_name=path.name,
        media_type=entry.media_type,
        source_uri="file:///mutable/local/path",
        created_at=now,
        created_by="test",
    )
    version = AssetVersionRow(
        asset_version_id=entry.asset_version_id,
        asset_id=entry.asset_id,
        version_identity="v1",
        content_hash=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        original_name=path.name,
        media_type=entry.media_type,
        source_uri=asset.source_uri,
        blob_uri="https://storage.example/indexed",
        blob_version_id="v1",
        landing_path="indexed.html",
        registered_at=now,
        landing_timestamp=now,
        ingestion_status="ready",
    )

    reader = IndexedSourceCorpusReader(
        source_root=tmp_path,
        assets=(asset,),
        versions=(version,),
    )
    resolved = reader.read(entry)

    assert resolved.elements
    assert resolved.elements[0].locator.source_uri is None
    assert resolved.elements[0].locator.blob_uri == version.blob_uri

    path.write_text("<p>changed landed bytes</p>", encoding="utf-8")
    with pytest.raises(L2StageError) as exc_info:
        reader.read(entry)
    assert exc_info.value.code == "L2_ASSET_CONTENT_MISMATCH"
