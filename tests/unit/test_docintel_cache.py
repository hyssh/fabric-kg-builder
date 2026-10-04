"""Offline tests of immutable raw-layout caching and verified text-page views."""

from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256, thaw_json
from fabric_kg_builder.sources.docintel_cache import (
    CachedLayout,
    DocIntelCacheError,
    UnsupportedLayoutCacheVersion,
    layout_text_pages,
    load_cached_layout,
    make_cached_layout,
    store_cached_layout,
    validate_extractor_identity,
)


def _identity():
    return {
        "endpoint": "https://example.cognitiveservices.azure.com",
        "api_version": "2024-11-30",
        "model_id": "prebuilt-layout",
        "options": {"output_content_format": "markdown", "features": ["ocrHighResolution"]},
    }


def _raw():
    return {
        "apiVersion": "2024-11-30",
        "modelId": "prebuilt-layout",
        "stringIndexType": "unicodeCodePoint",
        "content": "Page one.\nPage two.",
        "pages": [
            {
                "pageNumber": 3,
                "width": 8.5,
                "height": 11.0,
                "unit": "inch",
                "spans": [{"offset": 0, "length": 10}],
                "words": [{"content": "Page", "polygon": [0.1, 0.1, 0.4, 0.1, 0.4, 0.2, 0.1, 0.2]}],
            },
            {"pageNumber": 7, "spans": [{"offset": 10, "length": 9}]},
        ],
        "tables": [{
            "rowCount": 1,
            "columnCount": 1,
            "cells": [{
                "rowIndex": 0,
                "columnIndex": 0,
                "content": "Page",
                "boundingRegions": [{"pageNumber": 3, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
            }],
        }],
        "figures": [{"id": "3.1", "spans": [{"offset": 0, "length": 4}]}],
        "paragraphs": [{"content": "Page one.", "role": "sectionHeading"}],
    }


class _SdkResult:
    def __init__(self, value):
        self.value = value

    def as_dict(self):
        return copy.deepcopy(self.value)


def test_complete_raw_layout_roundtrip_and_verified_pages(tmp_path):
    raw = _raw()
    record = make_cached_layout(b"\x00raw PDF bytes\xff", _SdkResult(raw), _identity())
    path = store_cached_layout(tmp_path / "cache", record)
    loaded = load_cached_layout(
        tmp_path / "cache",
        input_sha256=record.input_sha256,
        extractor_identity=_identity(),
    )
    assert loaded == record
    assert path.name == f"{record.cache_key}.json"
    assert thaw_json(loaded.analyze_result) == raw
    assert loaded.response_hash == hashlib.sha256(loaded.analyze_result_json.encode("ascii")).hexdigest()
    assert layout_text_pages(loaded) == ((3, "Page one.\n"), (7, "Page two."))
    assert json.loads(loaded.model_dump(mode="json")["analyze_result_json"]) == raw
    assert loaded.contract_version == "1.1.0"


def test_installed_sdk_layout_roundtrip_preserves_wire_keys_and_geometry(tmp_path):
    from azure.ai.documentintelligence.models import (
        AnalyzeResult,
        BoundingRegion,
        DocumentPage,
        DocumentSpan,
        DocumentTable,
        DocumentTableCell,
    )

    result = AnalyzeResult(
        api_version="2024-11-30",
        model_id="prebuilt-layout",
        string_index_type="unicodeCodePoint",
        content="Page one.\nPage two.",
        pages=[
            DocumentPage(
                page_number=3,
                width=8.5,
                height=11.0,
                unit="inch",
                spans=[DocumentSpan(offset=0, length=10)],
            ),
            DocumentPage(page_number=7, spans=[DocumentSpan(offset=10, length=9)]),
        ],
        tables=[
            DocumentTable(
                row_count=1,
                column_count=1,
                cells=[
                    DocumentTableCell(
                        row_index=0,
                        column_index=0,
                        content="Page",
                        bounding_regions=[
                            BoundingRegion(
                                page_number=3, polygon=[0, 0, 1, 0, 1, 1, 0, 1]
                            )
                        ],
                    )
                ],
            )
        ],
    )
    raw = result.as_dict()
    assert raw["modelId"] == "prebuilt-layout"
    assert raw["pages"][0]["pageNumber"] == 3
    assert "page_number" not in raw["pages"][0]
    assert raw["tables"][0]["cells"][0]["boundingRegions"][0]["pageNumber"] == 3
    record = make_cached_layout(b"SDK input bytes", result, _identity())
    store_cached_layout(tmp_path, record)
    loaded = load_cached_layout(
        tmp_path, input_sha256=record.input_sha256, extractor_identity=_identity()
    )
    assert thaw_json(loaded.analyze_result) == raw
    assert layout_text_pages(loaded) == ((3, "Page one.\n"), (7, "Page two."))
    assert result.as_dict() == raw


def test_input_sha_is_raw_bytes_not_normalized_text():
    first = make_cached_layout("e\u0301".encode(), _raw(), _identity())
    second = make_cached_layout("é".encode(), _raw(), _identity())
    assert first.input_sha256 == hashlib.sha256(b"e\xcc\x81").hexdigest()
    assert first.input_sha256 != second.input_sha256
    assert first.cache_key != second.cache_key


def test_every_extractor_identity_option_changes_exact_key(tmp_path):
    original = make_cached_layout(b"same", _raw(), _identity())
    store_cached_layout(tmp_path, original)
    for field, value in (
        ("endpoint", "https://another.example.com"),
        ("api_version", "2023-07-31"),
        ("model_id", "different-model"),
        ("options", {"output_content_format": "text"}),
    ):
        identity = {**_identity(), field: value}
        changed = make_cached_layout(b"same", _raw(), identity)
        assert changed.cache_key != original.cache_key
        assert load_cached_layout(
            tmp_path, input_sha256=original.input_sha256, extractor_identity=identity
        ) is None
    assert load_cached_layout(
        tmp_path, input_sha256=hashlib.sha256(b"other").hexdigest(),
        extractor_identity=_identity(),
    ) is None


def test_miss_does_not_create_directory_or_analyze(tmp_path):
    root = tmp_path / "absent"
    assert load_cached_layout(
        root, input_sha256=hashlib.sha256(b"raw").hexdigest(), extractor_identity=_identity()
    ) is None
    assert not root.exists()


def test_identical_writes_are_idempotent_and_conflicts_never_overwrite(tmp_path):
    record = make_cached_layout(b"raw", _raw(), _identity())
    path = store_cached_layout(tmp_path, record)
    original, inode = path.read_bytes(), path.stat().st_ino
    assert store_cached_layout(tmp_path, record) == path
    changed = make_cached_layout(b"raw", {**_raw(), "content": "changed"}, _identity())
    with pytest.raises(DocIntelCacheError, match="different response"):
        store_cached_layout(tmp_path, changed)
    assert path.read_bytes() == original
    assert path.stat().st_ino == inode
    assert list(tmp_path.glob("*.pending")) == []


def test_concurrent_identical_create_only_publications(tmp_path):
    record = make_cached_layout(b"raw", _raw(), _identity())
    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(lambda _: store_cached_layout(tmp_path, record), range(8)))
    assert len(set(paths)) == 1
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("mutation", ["response", "hash", "version", "wrong-key", "whitespace"])
def test_corrupt_entry_refuses_read_and_replacement(tmp_path, mutation):
    record = make_cached_layout(b"raw", _raw(), _identity())
    path = store_cached_layout(tmp_path, record)
    payload = json.loads(path.read_text())
    if mutation == "response":
        raw = json.loads(payload["analyze_result_json"])
        raw["pages"][0]["width"] = 99
        payload["analyze_result_json"] = json.dumps(
            raw, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    elif mutation == "hash":
        payload["response_hash"] = "0" * 64
    elif mutation == "version":
        payload["contract_version"] = "2.0.0"
    elif mutation == "wrong-key":
        payload = make_cached_layout(b"other", _raw(), _identity()).model_dump(mode="json")
    path.write_text(json.dumps(payload))
    corrupt = path.read_bytes()
    with pytest.raises(DocIntelCacheError):
        load_cached_layout(
            tmp_path, input_sha256=record.input_sha256, extractor_identity=_identity()
        )
    with pytest.raises(DocIntelCacheError):
        store_cached_layout(tmp_path, record)
    assert path.read_bytes() == corrupt


@pytest.mark.parametrize("result", [
    None, [], {}, {"content": None}, {"content": 3},
    {"content": "text", "score": float("nan")},
    {"content": "text", "score": float("inf")},
    {"content": "text", "extra": object()},
    _SdkResult([]),
])
def test_invalid_raw_results_fail_without_sanitizing(result):
    with pytest.raises(DocIntelCacheError):
        make_cached_layout(b"raw", result, _identity())


@pytest.mark.parametrize("unsafe", [
    {"api_key": "short"},
    {"apiKey": "short"},
    {"key_file": "local-path"},
    {"nested": [{"password": "short"}]},
    {"nested": {"token": "short"}},
    {"endpoint": "https://user:password@example.com"},
    {"endpoint": "https://example.com?sig=abc"},
    {"endpoint": "https://example.com?api-key=abc"},
    {"header": "Bearer abc"},
])
def test_secret_bearing_identities_are_rejected_recursively(unsafe, tmp_path):
    identity = {**_identity(), "options": unsafe}
    with pytest.raises(DocIntelCacheError):
        validate_extractor_identity(identity)
    with pytest.raises(ValueError):
        make_cached_layout(b"raw", _raw(), identity)
    with pytest.raises(ValueError):
        load_cached_layout(
            tmp_path, input_sha256=hashlib.sha256(b"raw").hexdigest(),
            extractor_identity=identity,
        )
    assert list(tmp_path.iterdir()) == []


def test_standalone_identity_validation_is_pure_and_preserves_input():
    identity = _identity()
    original = copy.deepcopy(identity)
    assert validate_extractor_identity(identity) is None
    assert identity == original
    for invalid in ({}, None, [], {"options": {"score": float("nan")}}):
        with pytest.raises(DocIntelCacheError):
            validate_extractor_identity(invalid)


def test_response_and_identity_are_deeply_immutable_and_hash_checked():
    raw, identity = _raw(), _identity()
    record = make_cached_layout(b"raw", raw, identity)
    raw["pages"][0]["width"] = 99
    identity["options"]["features"].append("another-feature")
    assert record.analyze_result["pages"][0]["width"] == 8.5
    assert record.extractor_identity["options"]["features"] == ("ocrHighResolution",)
    with pytest.raises(TypeError):
        record.analyze_result["pages"][0]["width"] = 99
    with pytest.raises(ValueError):
        record.model_copy(update={"response_hash": "0" * 64})
    with pytest.raises(ValueError):
        CachedLayout.model_validate({**record.model_dump(), "extra": "not allowed"})


def test_nonsecret_endpoint_hash_identity_is_allowed():
    identity = {**_identity(), "endpoint_identity": hashlib.sha256(b"endpoint").hexdigest()}
    record = make_cached_layout(b"raw", _raw(), identity)
    assert thaw_json(record.extractor_identity) == identity


def test_lookup_does_not_normalize_a_different_identity(tmp_path):
    with pytest.raises(DocIntelCacheError):
        load_cached_layout(
            tmp_path,
            input_sha256=hashlib.sha256(b"raw").hexdigest(),
            extractor_identity={**_identity(), "options": {"description": "e\u0301"}},
        )


def test_symlink_entry_is_not_followed_or_replaced(tmp_path):
    record = make_cached_layout(b"raw", _raw(), _identity())
    outside = tmp_path / "separate.json"
    outside.write_text("unchanged")
    (tmp_path / f"{record.cache_key}.json").symlink_to(outside)
    with pytest.raises(DocIntelCacheError):
        store_cached_layout(tmp_path, record)
    assert outside.read_text() == "unchanged"


@pytest.mark.parametrize("pages", [None, [], [{"pageNumber": 9}], [{"spans": []}]])
def test_spanless_full_content_never_fabricates_page_number(pages):
    raw = {"content": "full content"}
    if pages is not None:
        raw["pages"] = pages
    assert layout_text_pages(make_cached_layout(b"raw", raw, _identity())) == ((None, "full content"),)


def test_empty_content_remains_explicitly_unsupported():
    record = make_cached_layout(b"image", {"content": "", "pages": []}, _identity())
    assert layout_text_pages(record) == ()


@pytest.mark.parametrize("update", [
    {"pageNumber": None},
    {"pageNumber": True},
    {"pageNumber": 0},
    {"spans": {}},
    {"spans": [{"offset": -1, "length": 2}]},
    {"spans": [{"offset": 0, "length": 1000}]},
    {"spans": [{"offset": True, "length": 2}]},
    {"spans": [{"offset": "0", "length": 2}]},
    {"spans": [{"offset": 0}]},
    {"spans": [{"offset": 0, "length": 2}, {"offset": 1, "length": 2}]},
    {"spans": [{"offset": 4, "length": 2}, {"offset": 0, "length": 2}]},
])
def test_malformed_spans_never_fallback_or_fabricate_pages(update):
    raw = _raw()
    raw["pages"][0].update(update)
    with pytest.raises(DocIntelCacheError):
        layout_text_pages(make_cached_layout(b"raw", raw, _identity()))


def test_missing_partial_page_spans_and_duplicate_page_numbers_fail():
    for update in ({"spans": []}, {"pageNumber": 3}):
        raw = _raw()
        raw["pages"][1].update(update)
        with pytest.raises(DocIntelCacheError):
            layout_text_pages(make_cached_layout(b"raw", raw, _identity()))


@pytest.mark.parametrize(("index_type", "offset", "length"), [
    ("unicodeCodePoint", 1, 1), ("utf16CodeUnit", 1, 2),
])
def test_unicode_span_units_are_verified(index_type, offset, length):
    raw = {
        "content": "A😀B", "stringIndexType": index_type,
        "pages": [{"pageNumber": 8, "spans": [{"offset": offset, "length": length}]}],
    }
    assert layout_text_pages(make_cached_layout(b"raw", raw, _identity())) == ((8, "😀"),)


def test_ambiguous_graphemes_and_split_utf16_surrogates_fail():
    for index_type in ("textElements", "utf16CodeUnit", "unknown"):
        raw = {
            "content": "A😀B", "stringIndexType": index_type,
            "pages": [{"pageNumber": 8, "spans": [{"offset": 1, "length": 1}]}],
        }
        with pytest.raises(DocIntelCacheError):
            layout_text_pages(make_cached_layout(b"raw", raw, _identity()))


def test_snake_case_layout_dictionary_pages():
    raw = {
        "content": "text", "string_index_type": "unicodeCodePoint",
        "pages": [{"page_number": 4, "spans": [{"offset": 0, "length": 4}]}],
    }
    assert layout_text_pages(make_cached_layout(b"raw", raw, _identity())) == ((4, "text"),)


@pytest.mark.parametrize("index_type", ["unicodeCodePoint", "utf16CodeUnit"])
def test_decomposed_raw_text_and_keys_preserve_original_spans(tmp_path, index_type):
    content = "Cafe\u0301 + e\u0301"
    raw = {
        "content": content,
        "stringIndexType": index_type,
        "pages": [{"pageNumber": 2, "spans": [{"offset": 8, "length": 2}]}],
        "e\u0301": {"name": "cafe\u0301"},
        "é": {"name": "distinct key"},
    }
    record = make_cached_layout(b"unchanged PDF", raw, _identity())
    expected = json.dumps(
        raw, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    assert record.analyze_result_json == expected
    assert record.response_hash == hashlib.sha256(expected.encode("ascii")).hexdigest()
    with pytest.raises(ValueError, match="duplicate object keys"):
        canonical_sha256(raw)
    path = store_cached_layout(tmp_path, record)
    loaded = load_cached_layout(
        tmp_path, input_sha256=record.input_sha256, extractor_identity=_identity()
    )
    assert thaw_json(loaded.analyze_result) == raw
    assert len([key for key in loaded.analyze_result if key in {"é", "e\u0301"}]) == 2
    assert layout_text_pages(loaded) == ((2, "e\u0301"),)
    assert json.loads(path.read_text())["analyze_result_json"] == expected


def test_composed_and_decomposed_responses_do_not_share_response_hash(tmp_path):
    first = make_cached_layout(b"same input", {"content": "e\u0301"}, _identity())
    second = make_cached_layout(b"same input", {"content": "é"}, _identity())
    assert first.cache_key == second.cache_key
    assert first.response_hash != second.response_hash
    path = store_cached_layout(tmp_path, first)
    original = path.read_bytes()
    with pytest.raises(DocIntelCacheError, match="different response"):
        store_cached_layout(tmp_path, second)
    assert path.read_bytes() == original


def test_changed_lossless_raw_bytes_are_detected(tmp_path):
    record = make_cached_layout(b"raw", {"content": "e\u0301"}, _identity())
    path = store_cached_layout(tmp_path, record)
    payload = json.loads(path.read_text())
    payload["analyze_result_json"] = payload["analyze_result_json"].replace(
        "e\\u0301", "\\u00e9"
    )
    path.write_text(canonical_json(payload) + "\n")
    with pytest.raises(DocIntelCacheError):
        load_cached_layout(
            tmp_path, input_sha256=record.input_sha256, extractor_identity=_identity()
        )


def test_legacy_cache_version_is_preserved_and_never_silently_resealed(tmp_path):
    record = make_cached_layout(b"raw", _raw(), _identity())
    legacy = record.model_dump(mode="json")
    legacy.pop("analyze_result_json")
    legacy.update(
        contract_version="1.0.0",
        analyze_result=_raw(),
        response_hash=canonical_sha256(_raw()),
    )
    encoded = (canonical_json(legacy) + "\n").encode()
    path = tmp_path / f"{record.cache_key}.json"
    path.write_bytes(encoded)
    with pytest.raises(UnsupportedLayoutCacheVersion):
        load_cached_layout(
            tmp_path, input_sha256=record.input_sha256, extractor_identity=_identity()
        )
    with pytest.raises(UnsupportedLayoutCacheVersion):
        store_cached_layout(tmp_path, record)
    assert path.read_bytes() == encoded


def test_installed_sdk_decomposed_text_roundtrip(tmp_path):
    from azure.ai.documentintelligence.models import AnalyzeResult, DocumentPage, DocumentSpan

    result = AnalyzeResult(
        api_version="2024-11-30",
        model_id="prebuilt-layout",
        string_index_type="unicodeCodePoint",
        content="Cafe\u0301",
        pages=[DocumentPage(page_number=6, spans=[DocumentSpan(offset=0, length=5)])],
    )
    raw = result.as_dict()
    record = make_cached_layout(b"raw SDK input", result, _identity())
    store_cached_layout(tmp_path, record)
    loaded = load_cached_layout(
        tmp_path, input_sha256=record.input_sha256, extractor_identity=_identity()
    )
    assert thaw_json(loaded.analyze_result) == raw
    assert layout_text_pages(loaded) == ((6, "Cafe\u0301"),)
    assert loaded.analyze_result["content"] != "Café"
