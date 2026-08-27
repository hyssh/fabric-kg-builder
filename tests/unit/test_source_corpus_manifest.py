from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fabric_kg_builder.domain.stage import make_l1_identity
from fabric_kg_builder.sources.adapter import AdapterError, FailureType
from fabric_kg_builder.sources.corpus import (
    build_source_corpus_manifest,
    extract_verified_source_snapshot,
    read_verified_source_snapshot,
    validate_corpus_manifest_against_source,
)
from fabric_kg_builder.sources.inspector import build_l1_design_artifacts


def test_complete_corpus_retains_supported_and_unsupported_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "records.txt").write_text("records", encoding="utf-8")
    (tmp_path / "unknown.bin").write_bytes(b"\x00\x01")
    identity = make_l1_identity(project_id="project:test", run_id="run:test")

    manifest = build_source_corpus_manifest(
        tmp_path,
        corpus_root_id="corpus-root:test",
        identity=identity,
    )

    assert manifest.total_entry_count == 2
    assert (
        manifest.eligible_entry_count
        + manifest.excluded_entry_count
        + manifest.blocked_entry_count
        == 2
    )
    assert [item.source_file_id for item in manifest.entries] == sorted(
        item.source_file_id for item in manifest.entries
    )


def test_corpus_reconciliation_detects_added_or_changed_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    path = source / "records.txt"
    path.write_text("version one", encoding="utf-8")
    identity = make_l1_identity(project_id="project:test", run_id="run:test")
    manifest = build_source_corpus_manifest(
        source,
        corpus_root_id="corpus-root:test",
        identity=identity,
    )
    path.write_text("version two", encoding="utf-8")

    with pytest.raises(ValueError, match="differ"):
        validate_corpus_manifest_against_source(
            manifest,
            source,
            identity=identity,
        )


def _manifest_for(path: Path):
    identity = make_l1_identity(project_id="project:test", run_id="run:test")
    return (
        identity,
        build_source_corpus_manifest(
            path,
            corpus_root_id="corpus-root:test",
            identity=identity,
        ),
    )


@pytest.mark.parametrize("replacement", [b"bravo", b"z", b"alpha-appended"])
def test_l1_snapshot_fails_closed_on_path_byte_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bytes,
) -> None:
    path = tmp_path / "source.md"
    path.write_bytes(b"alpha")
    identity, manifest = _manifest_for(path)
    replacement_path = tmp_path / "replacement.md"
    replacement_path.write_bytes(replacement)
    os.replace(replacement_path, path)
    extraction_calls = 0

    def unexpected_extract(*args, **kwargs):
        nonlocal extraction_calls
        extraction_calls += 1
        raise AssertionError("changed bytes must fail before extraction")

    monkeypatch.setattr(
        "fabric_kg_builder.sources.inspector.extract_verified_source_snapshot",
        unexpected_extract,
    )

    with pytest.raises(AdapterError) as exc_info:
        build_l1_design_artifacts(
            path,
            corpus=manifest,
            base_identity=identity,
            verified_at_utc=datetime.now(timezone.utc),
        )

    assert exc_info.value.failure_type is FailureType.CORRUPT
    assert extraction_calls == 0


def test_l1_snapshot_fails_closed_on_symlink_target_swap(tmp_path: Path) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    source = tmp_path / "source.md"
    first.write_bytes(b"first")
    second.write_bytes(b"other")
    source.symlink_to(first)
    identity, manifest = _manifest_for(source)
    source.unlink()
    source.symlink_to(second)

    with pytest.raises(AdapterError) as exc_info:
        build_l1_design_artifacts(
            source,
            corpus=manifest,
            base_identity=identity,
            verified_at_utc=datetime.now(timezone.utc),
        )

    assert exc_info.value.failure_type is FailureType.CORRUPT


def test_snapshot_detects_in_place_mutation_during_streamed_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.md"
    path.write_bytes(b"abcdefgh")
    _, manifest = _manifest_for(path)
    monkeypatch.setattr(
        "fabric_kg_builder.sources.corpus._SNAPSHOT_CHUNK_BYTES",
        4,
    )
    mutated = False

    def mutate_after_first_chunk(byte_count: int) -> None:
        nonlocal mutated
        if byte_count == 4 and not mutated:
            mutated = True
            path.write_bytes(b"abcdWXYZ")

    with pytest.raises(AdapterError) as exc_info:
        read_verified_source_snapshot(
            path,
            entry=manifest.entries[0],
            corpus_root_id=manifest.corpus_root_id,
            _read_hook=mutate_after_first_chunk,
        )

    assert mutated
    assert exc_info.value.failure_type is FailureType.CORRUPT


def test_verified_snapshot_is_extracted_once_without_reopening_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    _, manifest = _manifest_for(path)
    snapshot = read_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
    )
    path.write_text("<p>replacement source text</p>", encoding="utf-8")

    from fabric_kg_builder.sources import router

    real_extract = router.extract
    extraction_calls = 0

    def counted_extract(snapshot_path: Path):
        nonlocal extraction_calls
        extraction_calls += 1
        assert snapshot_path != path
        return real_extract(snapshot_path)

    monkeypatch.setattr(router, "extract", counted_extract)
    result = extract_verified_source_snapshot(snapshot)

    assert extraction_calls == 1
    assert result.document_elements
    assert "immutable source text" in result.document_elements[0].content


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("byte_count", 1),
        ("original_byte_hash", "0" * 64),
        ("source_file_id", "source-file:wrong"),
        ("asset_id", "asset:wrong"),
        ("asset_version_id", "asset-version:wrong"),
        ("media_type", "application/pdf"),
    ],
)
def test_snapshot_rejects_mismatched_sealed_metadata(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    _, manifest = _manifest_for(path)
    entry = manifest.entries[0].model_copy(update={field: value})

    with pytest.raises(AdapterError) as exc_info:
        read_verified_source_snapshot(
            path,
            entry=entry,
            corpus_root_id=manifest.corpus_root_id,
        )

    assert exc_info.value.failure_type is FailureType.CORRUPT


def test_snapshot_rejects_mismatched_sealed_adapter_kind(tmp_path: Path) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    _, manifest = _manifest_for(path)
    snapshot = read_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
    )

    with pytest.raises(AdapterError) as exc_info:
        extract_verified_source_snapshot(
            replace(snapshot, adapter_name="pdf_extractor")
        )

    assert exc_info.value.failure_type is FailureType.MIME_MISMATCH


def test_l1_fails_closed_on_mismatched_sealed_adapter_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    identity, manifest = _manifest_for(path)
    snapshot = read_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
    )
    monkeypatch.setattr(
        "fabric_kg_builder.sources.inspector.read_verified_source_snapshot",
        lambda *args, **kwargs: replace(snapshot, adapter_name="pdf_extractor"),
    )

    with pytest.raises(AdapterError) as exc_info:
        build_l1_design_artifacts(
            path,
            corpus=manifest,
            base_identity=identity,
            verified_at_utc=datetime.now(timezone.utc),
        )

    assert exc_info.value.failure_type is FailureType.MIME_MISMATCH


def test_l1_immutable_file_binds_evidence_to_snapshot_hash(tmp_path: Path) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>A governed immutable source.</p>", encoding="utf-8")
    identity, manifest = _manifest_for(path)

    _, _, source_units, evidence_spans = build_l1_design_artifacts(
        path,
        corpus=manifest,
        base_identity=identity,
        verified_at_utc=datetime.now(timezone.utc),
    )

    entry = manifest.entries[0]
    assert source_units
    assert evidence_spans
    assert all(unit.identity.content_hash == entry.original_byte_hash for unit in source_units)
    assert all(unit.identity.asset_version_id == entry.asset_version_id for unit in source_units)
    assert all(span.asset_version_id == entry.asset_version_id for span in evidence_spans)
