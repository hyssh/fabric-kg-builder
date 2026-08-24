from __future__ import annotations

from pathlib import Path

import pytest

from fabric_kg_builder.domain.stage import make_l1_identity
from fabric_kg_builder.sources.corpus import (
    build_source_corpus_manifest,
    validate_corpus_manifest_against_source,
)


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
