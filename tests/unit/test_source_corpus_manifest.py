from __future__ import annotations

import os
import stat
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fabric_kg_builder.domain.stage import make_l1_identity
from fabric_kg_builder.sources.adapter import AdapterError, FailureType
from fabric_kg_builder.sources.corpus import (
    build_source_corpus_manifest,
    extract_verified_source_snapshot,
    open_verified_source_snapshot,
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
        with open_verified_source_snapshot(
            path,
            entry=manifest.entries[0],
            corpus_root_id=manifest.corpus_root_id,
            _read_hook=mutate_after_first_chunk,
        ):
            pass

    assert mutated
    assert exc_info.value.failure_type is FailureType.CORRUPT


def test_verified_snapshot_is_extracted_once_without_reopening_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    _, manifest = _manifest_for(path)
    with open_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
    ) as snapshot:
        path.write_text("<p>replacement source text</p>", encoding="utf-8")
        assert snapshot.path.name == path.name

        from fabric_kg_builder.sources import router

        real_extract = router.extract_with_adapter
        extraction_calls = 0

        def counted_extract(snapshot_path: Path, adapter_name: str):
            nonlocal extraction_calls
            extraction_calls += 1
            assert snapshot_path != path
            return real_extract(snapshot_path, adapter_name)

        monkeypatch.setattr(router, "extract_with_adapter", counted_extract)
        extraction = extract_verified_source_snapshot(snapshot)

    assert extraction_calls == 1
    assert extraction.adapter_result.document_elements
    assert extraction.adapter_result.source_file.filename == path.name
    assert (
        "immutable source text"
        in extraction.adapter_result.document_elements[0].content
    )
    assert extraction.consumed_byte_hash == manifest.entries[0].original_byte_hash
    assert extraction.consumed_byte_count == manifest.entries[0].byte_count


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
        with open_verified_source_snapshot(
            path,
            entry=entry,
            corpus_root_id=manifest.corpus_root_id,
        ):
            pass

    assert exc_info.value.failure_type is FailureType.CORRUPT


def test_snapshot_rejects_mismatched_sealed_adapter_kind(tmp_path: Path) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    _, manifest = _manifest_for(path)
    with open_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
    ) as snapshot:
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
    snapshot_context = open_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
    )
    with snapshot_context as snapshot:
        from contextlib import contextmanager

        @contextmanager
        def tampered_snapshot(*args, **kwargs):
            yield replace(snapshot, adapter_name="pdf_extractor")

        monkeypatch.setattr(
            "fabric_kg_builder.sources.inspector.open_verified_source_snapshot",
            tampered_snapshot,
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


def test_snapshot_streaming_has_bounded_chunks_and_no_byte_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk_size = 64 * 1024
    path = tmp_path / "source.md"
    path.write_bytes(b"x" * (chunk_size * 9 + 17))
    _, manifest = _manifest_for(path)
    monkeypatch.setattr(
        "fabric_kg_builder.sources.corpus._SNAPSHOT_CHUNK_BYTES",
        chunk_size,
    )
    cumulative: list[int] = []

    with open_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
        _read_hook=cumulative.append,
    ) as snapshot:
        assert snapshot.byte_count == path.stat().st_size
        assert "original_bytes" not in {field.name for field in fields(snapshot)}

    increments = [
        current - previous
        for previous, current in zip([0, *cumulative], cumulative)
    ]
    assert increments
    assert max(increments) <= chunk_size


def test_snapshot_path_replacement_fails_before_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    _, manifest = _manifest_for(path)
    adapter_calls = 0

    from fabric_kg_builder.sources import router

    def unexpected_adapter(*args, **kwargs):
        nonlocal adapter_calls
        adapter_calls += 1
        raise AssertionError("replaced snapshot must fail before adapter")

    monkeypatch.setattr(router, "extract_with_adapter", unexpected_adapter)

    def replace_path(phase, snapshot) -> None:
        if phase == "before_adapter":
            replacement = snapshot.path.with_name("replacement.md")
            replacement.write_bytes(b"x" * snapshot.byte_count)
            os.replace(replacement, snapshot.path)

    with open_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
    ) as snapshot:
        with pytest.raises(AdapterError) as exc_info:
            extract_verified_source_snapshot(
                snapshot,
                _consume_hook=replace_path,
            )

    assert exc_info.value.failure_type is FailureType.CORRUPT
    assert adapter_calls == 0


def test_snapshot_same_inode_mutation_between_route_and_adapter_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    _, manifest = _manifest_for(path)
    adapter_calls = 0

    from fabric_kg_builder.sources import router

    def unexpected_adapter(*args, **kwargs):
        nonlocal adapter_calls
        adapter_calls += 1
        raise AssertionError("mutated snapshot must fail before adapter")

    monkeypatch.setattr(router, "extract_with_adapter", unexpected_adapter)

    def mutate_inode(phase, snapshot) -> None:
        if phase == "before_adapter":
            os.chmod(snapshot.path, 0o600)
            snapshot.path.write_bytes(b"z" * snapshot.byte_count)

    with open_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
    ) as snapshot:
        with pytest.raises(AdapterError) as exc_info:
            extract_verified_source_snapshot(
                snapshot,
                _consume_hook=mutate_inode,
            )

    assert exc_info.value.failure_type is FailureType.CORRUPT
    assert adapter_calls == 0


def test_snapshot_mutation_during_adapter_read_discards_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    _, manifest = _manifest_for(path)

    from fabric_kg_builder.sources import router

    real_extract = router.extract_with_adapter

    def mutating_adapter(snapshot_path: Path, adapter_name: str):
        with snapshot_path.open("rb") as stream:
            assert stream.read(4)
        os.chmod(snapshot_path, 0o600)
        snapshot_path.write_bytes(b"z" * snapshot_path.stat().st_size)
        return real_extract(snapshot_path, adapter_name)

    monkeypatch.setattr(router, "extract_with_adapter", mutating_adapter)

    with open_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
    ) as snapshot:
        with pytest.raises(AdapterError) as exc_info:
            extract_verified_source_snapshot(snapshot)

    assert exc_info.value.failure_type is FailureType.CORRUPT


def test_l1_temp_snapshot_mutation_emits_no_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    identity, manifest = _manifest_for(path)

    from fabric_kg_builder.sources.corpus import (
        extract_verified_source_snapshot as guarded_extract,
    )

    def mutate_after_adapter(snapshot):
        def mutate(phase, guarded_snapshot) -> None:
            if phase == "after_adapter":
                os.chmod(guarded_snapshot.path, 0o600)
                guarded_snapshot.path.write_bytes(
                    b"z" * guarded_snapshot.byte_count
                )

        return guarded_extract(snapshot, _consume_hook=mutate)

    monkeypatch.setattr(
        "fabric_kg_builder.sources.inspector.extract_verified_source_snapshot",
        mutate_after_adapter,
    )

    with pytest.raises(AdapterError) as exc_info:
        build_l1_design_artifacts(
            path,
            corpus=manifest,
            base_identity=identity,
            verified_at_utc=datetime.now(timezone.utc),
        )

    assert exc_info.value.failure_type is FailureType.CORRUPT


def test_l1_adapter_corruption_remains_a_profile_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>corrupt adapter fixture</p>", encoding="utf-8")
    identity, manifest = _manifest_for(path)

    def corrupt_adapter(snapshot):
        raise AdapterError(
            FailureType.CORRUPT,
            "adapter could not parse source",
            source_locator=snapshot.entry.relative_source_ref,
        )

    monkeypatch.setattr(
        "fabric_kg_builder.sources.inspector.extract_verified_source_snapshot",
        corrupt_adapter,
    )

    sample, profile, source_units, evidence_spans = build_l1_design_artifacts(
        path,
        corpus=manifest,
        base_identity=identity,
        verified_at_utc=datetime.now(timezone.utc),
    )

    assert sample.entries == ()
    assert source_units == ()
    assert evidence_spans == ()
    assert [warning.warning_type for warning in profile.warnings] == ["corrupt"]


def test_private_snapshot_permissions_and_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "source.md"
    path.write_text("<p>immutable source text</p>", encoding="utf-8")
    _, manifest = _manifest_for(path)

    with open_verified_source_snapshot(
        path,
        entry=manifest.entries[0],
        corpus_root_id=manifest.corpus_root_id,
    ) as snapshot:
        snapshot_path = snapshot.path
        temp_root = snapshot_path.parent
        if os.name != "nt":
            assert stat.S_IMODE(temp_root.stat().st_mode) == 0o700
            assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o400

    assert not temp_root.exists()
