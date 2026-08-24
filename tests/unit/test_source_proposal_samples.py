from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from fabric_kg_builder.model.ids import (
    content_hash as compute_content_hash,
    make_document_element_id,
    make_source_file_id,
)
from fabric_kg_builder.model.schemas import DocumentElementRow, SourceFileRow
from fabric_kg_builder.domain.proposal import _evidence_from_profile
from fabric_kg_builder.sources.adapter import AdapterError, FailureType
from fabric_kg_builder.sources.inspector import (
    MAX_PROPOSAL_SAMPLES,
    MAX_SAMPLE_EXCERPT_CHARS,
    MAX_SAMPLE_EXCERPT_TOTAL_CHARS,
    build_source_profile,
    compute_source_profile_hash,
    load_source_profile,
)


def _write_html(path: Path, *, secret: str | None = None) -> Path:
    token_line = f"<p>Credential {secret}</p>" if secret else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "<html><body>"
            "<h1>Plant Overview</h1>"
            "<p>Main pump P-100 is offline and needs inspection.</p>"
            "<table><tr><th>Asset</th><th>Status</th></tr><tr><td>P-100</td><td>Offline</td></tr></table>"
            "<img src='overview.png' alt='Equipment layout overview' />"
            f"{token_line}"
            "</body></html>"
        ),
        encoding="utf-8",
    )
    return path


def _write_csv(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "asset_id,status,owner\nP-100,Offline,Operations\nP-200,Online,Maintenance\n",
        encoding="utf-8",
    )
    return path


def _fake_extract_result(
    path: Path,
    *,
    element_type: str,
    content: str,
    page_number: int | None = None,
    section_path: str | None = None,
    sort_order: int = 0,
) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    source_file_id = make_source_file_id(path.name, file_hash)
    source_file = SourceFileRow(
        source_file_id=source_file_id,
        path=path.name,
        filename=path.name,
        source_type=path.suffix.lstrip("."),
        content_hash=file_hash,
        byte_size=path.stat().st_size,
        ingested_at=now,
    )
    element_hash = compute_content_hash(content)
    element = DocumentElementRow(
        document_element_id=make_document_element_id(
            source_file_id,
            element_type,
            page_number,
            sort_order,
            element_hash,
        ),
        source_file_id=source_file_id,
        element_type=element_type,
        content=content,
        page_number=page_number,
        section_path=section_path,
        sort_order=sort_order,
        content_hash=element_hash,
        extracted_at=now,
    )
    return SimpleNamespace(source_file=source_file, document_elements=[element])


@pytest.mark.unit
def test_sampling_is_deterministic_bounded_and_ordered(tmp_path: Path) -> None:
    for idx in range(6):
        _write_html(tmp_path / f"report_{idx}.html")

    first = build_source_profile(tmp_path, include_proposal_samples=True)
    second = build_source_profile(tmp_path, include_proposal_samples=True)

    first_signature = [
        (sample.sample_id, sample.sample_kind, sample.citation_path, sample.excerpt, sample.content_hash)
        for sample in first.proposal_samples
    ]
    second_signature = [
        (sample.sample_id, sample.sample_kind, sample.citation_path, sample.excerpt, sample.content_hash)
        for sample in second.proposal_samples
    ]

    assert first_signature == second_signature
    assert len(first.proposal_samples) == MAX_PROPOSAL_SAMPLES
    assert sum(len(sample.excerpt) for sample in first.proposal_samples) <= MAX_SAMPLE_EXCERPT_TOTAL_CHARS
    assert all(len(sample.excerpt) <= MAX_SAMPLE_EXCERPT_CHARS for sample in first.proposal_samples)
    assert [sample.sample_kind for sample in first.proposal_samples[:4]] == ["heading"] * 4


@pytest.mark.unit
def test_sampling_includes_heading_text_table_with_stable_citations_and_hashes(tmp_path: Path) -> None:
    _write_html(tmp_path / "docs" / "overview.html")
    _write_csv(tmp_path / "data" / "assets.csv")

    profile = build_source_profile(tmp_path, include_proposal_samples=True)

    kinds = {sample.sample_kind for sample in profile.proposal_samples}
    citations = {sample.citation_path for sample in profile.proposal_samples}

    assert {"heading", "text", "table"} <= kinds
    assert {"docs/overview.html", "data/assets.csv"} <= citations
    assert all(sample.sample_id.startswith("sample:") for sample in profile.proposal_samples)
    assert all(not Path(sample.citation_path).is_absolute() for sample in profile.proposal_samples)
    assert all(".." not in sample.citation_path.split("/") for sample in profile.proposal_samples)
    assert all(sample.source_file_id for sample in profile.proposal_samples)
    assert all(
        sample.content_hash == hashlib.sha256(sample.excerpt.encode("utf-8")).hexdigest()
        for sample in profile.proposal_samples
    )


@pytest.mark.unit
def test_sampling_includes_visual_descriptions_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = tmp_path / "diagram.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    def _extract(path: Path) -> SimpleNamespace:
        return _fake_extract_result(
            Path(path),
            element_type="vision_description",
            content="Annotated pump room layout with two vessels.",
            sort_order=1,
        )

    monkeypatch.setattr("fabric_kg_builder.sources.inspector.router.extract", _extract)
    profile = build_source_profile(tmp_path, include_proposal_samples=True)

    assert len(profile.proposal_samples) == 1
    sample = profile.proposal_samples[0]
    assert sample.sample_kind == "visual"
    assert sample.element_type == "vision_description"
    assert sample.citation_path == "diagram.png"


@pytest.mark.unit
def test_schema_1_metadata_profile_does_not_run_proposal_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_html(tmp_path / "overview.html")

    def _unexpected_extract(_: Path) -> SimpleNamespace:
        raise AssertionError("schema-1 metadata inspection invoked a source adapter")

    monkeypatch.setattr(
        "fabric_kg_builder.sources.inspector.router.extract",
        _unexpected_extract,
    )
    profile = build_source_profile(tmp_path)

    assert profile.observed.total_file_count == 1
    assert profile.proposal_samples == []
    assert profile.sampling_warnings == []


@pytest.mark.unit
def test_sample_locator_free_text_is_redacted_before_persistence_and_model_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "b2" * 16
    source = _write_html(tmp_path / f"api-key-{secret}.html")

    def _extract(path: Path) -> SimpleNamespace:
        return _fake_extract_result(
            Path(path),
            element_type="paragraph",
            content="Pump P-100 requires inspection.",
            section_path=f"token/{secret} for the restricted section.",
            sort_order=1,
        )

    monkeypatch.setattr(
        "fabric_kg_builder.sources.inspector.router.extract",
        _extract,
    )
    profile = build_source_profile(source, include_proposal_samples=True)
    sample = profile.proposal_samples[0]

    assert secret not in sample.citation_path
    assert secret not in (sample.section_path or "")
    assert "[REDACTED]" in sample.citation_path
    assert "[REDACTED]" in (sample.section_path or "")

    # Defense in depth: model-input conversion redacts manually supplied legacy
    # sample locator values too.
    unredacted = sample.model_copy(
        update={"section_path": f"token={secret} for model input"}
    )
    evidence = _evidence_from_profile(
        profile.model_copy(update={"proposal_samples": [unredacted]})
    )
    assert secret not in json.dumps(evidence[0].locator)
    assert "[REDACTED]" in evidence[0].locator["section_path"]


@pytest.mark.unit
def test_generated_fkg_artifacts_are_root_ignored_without_fixture_exclusion() -> None:
    ignore_lines = (
        Path(__file__).resolve().parents[2] / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()

    assert "/.fkg/" in ignore_lines
    assert ".fkg/" not in ignore_lines
    fixture_path = Path("tests/fixtures/domain_proposals")
    assert ".fkg" not in fixture_path.parts


@pytest.mark.unit
def test_sampling_redacts_detected_secrets_without_dropping_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    _write_html(tmp_path / "secret.html", secret=secret)

    def _extract(path: Path) -> SimpleNamespace:
        return _fake_extract_result(
            Path(path),
            element_type="paragraph",
            content=f"Credential {secret}",
            sort_order=1,
        )

    monkeypatch.setattr("fabric_kg_builder.sources.inspector.router.extract", _extract)
    profile = build_source_profile(tmp_path, include_proposal_samples=True)
    excerpts = [sample.excerpt for sample in profile.proposal_samples if sample.sample_kind == "text"]

    assert any("[REDACTED]" in excerpt for excerpt in excerpts)
    assert all(secret not in excerpt for excerpt in excerpts)


@pytest.mark.unit
def test_sampling_redacts_labeled_key_with_trailing_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "a1" * 16
    source = _write_html(tmp_path / "secret-with-context.html")

    def _extract(path: Path) -> SimpleNamespace:
        return _fake_extract_result(
            Path(path),
            element_type="paragraph",
            content=f"API key: {secret} for production use only.",
            sort_order=1,
        )

    monkeypatch.setattr(
        "fabric_kg_builder.sources.inspector.router.extract",
        _extract,
    )
    profile = build_source_profile(source, include_proposal_samples=True)
    excerpt = profile.proposal_samples[0].excerpt

    assert secret not in excerpt
    assert "[REDACTED]" in excerpt
    assert "for production use only" in excerpt


@pytest.mark.unit
def test_sampling_failures_become_visible_typed_warnings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = _write_html(tmp_path / "broken.html")

    def _raise(_: Path) -> SimpleNamespace:
        raise AdapterError(FailureType.CORRUPT, "html parse failed", source_locator=str(broken))

    monkeypatch.setattr("fabric_kg_builder.sources.inspector.router.extract", _raise)
    profile = build_source_profile(tmp_path, include_proposal_samples=True)

    assert profile.observed.total_file_count == 1
    assert profile.proposal_samples == []
    assert len(profile.sampling_warnings) == 1
    warning = profile.sampling_warnings[0]
    assert warning.warning_type == "corrupt"
    assert warning.citation_path == "broken.html"
    assert "html parse failed" in warning.message


@pytest.mark.unit
def test_profile_hash_is_canonical_and_legacy_profiles_still_load(tmp_path: Path) -> None:
    _write_csv(tmp_path / "assets.csv")
    profile = build_source_profile(tmp_path, include_proposal_samples=True)

    base_hash = compute_source_profile_hash(profile)
    profile.approved = True
    profile.approved_by = "reviewer@example.com"
    profile.approved_at_utc = "2026-08-23T00:00:00Z"
    profile.inspected_at_utc = "2026-08-24T00:00:00Z"

    assert compute_source_profile_hash(profile) == base_hash

    legacy_payload = profile.model_dump(mode="json")
    legacy_payload.pop("proposal_samples", None)
    legacy_payload.pop("sampling_warnings", None)
    legacy_payload.pop("profile_hash", None)
    legacy_path = tmp_path / "legacy-source-profile.json"
    legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    loaded = load_source_profile(legacy_path)
    assert loaded.proposal_samples == []
    assert loaded.sampling_warnings == []
    assert loaded.source_hash == profile.source_hash
    assert loaded.profile_hash == compute_source_profile_hash(loaded)
    assert loaded.profile_hash
    assert loaded.profile_hash != base_hash
