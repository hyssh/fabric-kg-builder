"""Offline cached-source transport tests, not real-document acceptance."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.enrichment import schema2_sources
from fabric_kg_builder.enrichment.schema2_sources import (
    IndexedSourceCorpusReader, L2StageError, materialize_source_corpus,
)
from fabric_kg_builder.model.schemas import AssetRow, AssetVersionRow
from fabric_kg_builder.enrichment.docintel import DocIntelClient
from fabric_kg_builder.sources.docintel_cache import make_cached_layout, store_cached_layout
from fabric_kg_builder.sources.corpus import build_source_corpus_manifest
from tests.unit.test_docintel_cache import _identity as extractor_identity
from tests.unit.test_layout_cache_cmd import Client, Poller, pdf
from tests.unit.test_schema2_sources import _identity, _inputs


@pytest.fixture()
def cached_pdf(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    path = pdf(source, pages=2)
    corpus = build_source_corpus_manifest(
        path, corpus_root_id="corpus:cached-pdf-test", identity=_identity(),
    )
    entry = corpus.entries[0]
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    asset = AssetRow(
        asset_id=entry.asset_id, project_id=_identity().project_id,
        original_name=path.name, media_type=entry.media_type,
        source_uri="https://source.example/record.pdf", created_at=now, created_by="test",
    )
    version = AssetVersionRow(
        asset_version_id=entry.asset_version_id, asset_id=asset.asset_id,
        version_identity="v1", content_hash=entry.original_byte_hash,
        size_bytes=entry.byte_count, original_name=path.name, media_type=entry.media_type,
        source_uri=asset.source_uri, blob_uri="https://storage.example/record.pdf",
        blob_version_id="v1", landing_path=entry.relative_source_ref,
        registered_at=now, landing_timestamp=now, ingestion_status="ready",
    )
    raw = Poller().result(timeout=0)
    page_text = raw["content"]
    raw["content"] = page_text + "\n" + page_text
    raw["pages"] = [
        {
            **raw["pages"][0], "pageNumber": number,
            "spans": [{"offset": offset, "length": len(page_text)}],
        }
        for number, offset in [(1, 0), (2, len(page_text) + 1)]
    ]
    cache = tmp_path / "cache"
    identity = extractor_identity()
    fallback = Mock(side_effect=AssertionError("Cache mode must not invoke another adapter"))
    analysis = Mock(side_effect=AssertionError("Cache mode must never reanalyze"))
    monkeypatch.setattr(schema2_sources, "extract_verified_source_snapshot", fallback)
    monkeypatch.setattr(DocIntelClient, "layout_analyze_raw", analysis)

    def reader(identity_override=None):
        return IndexedSourceCorpusReader(
            source_root=source, assets=(asset,), versions=(version,),
            layout_cache=cache,
            layout_identity=identity if identity_override is None else identity_override,
        )

    yield SimpleNamespace(
        path=path, corpus=corpus, entry=entry, raw=raw, page_text=page_text,
        cache=cache, identity=identity, reader=reader,
    )
    fallback.assert_not_called()
    analysis.assert_not_called()


def test_exact_cached_pdf_materializes_every_page_with_cache_bound_locators(cached_pdf):
    case = cached_pdf
    record = make_cached_layout(case.path.read_bytes(), case.raw, case.identity)
    store_cached_layout(case.cache, record)
    result = materialize_source_corpus(inputs=_inputs(case.corpus), reader=case.reader())
    assert result.report.source_unit_count == 2
    assert result.report.materialized_corpus_entry_count == 1
    assert {unit.locator.page for unit in result.source_units} == {1, 2}
    assert {unit.text for unit in result.source_units} == {case.page_text}
    assert len({unit.source_unit_id for unit in result.source_units}) == 2
    for unit in result.source_units:
        assert unit.locator.native_object_id == f"{record.cache_key}/pages/{unit.locator.page}"
        assert unit.locator.blob_version_id == "v1"
        assert unit.identity.asset_version_id == case.entry.asset_version_id


def test_cached_pdf_requires_exact_extractor_options(cached_pdf):
    case = cached_pdf
    store_cached_layout(
        case.cache, make_cached_layout(case.path.read_bytes(), case.raw, case.identity),
    )
    changed = deepcopy(case.identity)
    changed["options"]["features"] = []
    with pytest.raises(L2StageError) as error:
        case.reader(changed).read(case.entry)
    assert error.value.code == "L2_LAYOUT_CACHE_MISSING"


def test_cached_pdf_rejects_partial_document_page_coverage(cached_pdf):
    case = cached_pdf
    partial = deepcopy(case.raw)
    partial["pages"] = partial["pages"][:1]
    partial["content"] = case.page_text
    store_cached_layout(
        case.cache, make_cached_layout(case.path.read_bytes(), partial, case.identity),
    )
    with pytest.raises(L2StageError) as error:
        case.reader().read(case.entry)
    assert error.value.code == "L2_LAYOUT_COVERAGE_INCOMPLETE"


def test_cached_pdf_miss_fails_closed_without_analysis_or_adapter_fallback(cached_pdf):
    with pytest.raises(L2StageError) as error:
        cached_pdf.reader().read(cached_pdf.entry)
    assert error.value.code == "L2_LAYOUT_CACHE_MISSING"
    assert not cached_pdf.cache.exists()


def test_public_dry_run_checks_cache_boundary_without_analysis_or_writes(cached_pdf, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = Client()
    result = CliRunner().invoke(cli, [
        "domain", "analyze-layout", "--input", str(cached_pdf.path),
        "--endpoint", "https://example.test", "--cache-dir", str(cached_pdf.cache),
        "--max-pages", "2",
    ], obj={"_docintel_client": client})
    assert result.exit_code == 0, result.output
    assert not client.calls
    assert not cached_pdf.cache.exists()
    cached_pdf.cache.mkdir()
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    result = CliRunner().invoke(cli, [
        "--dry-run", "enrich", "--input", str(cached_pdf.path),
        "--ocr-cache", str(cached_pdf.cache),
    ])
    assert result.exit_code == 2, result.output
    assert "--ocr-cache and --ocr-identity must be supplied together" in result.output
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
