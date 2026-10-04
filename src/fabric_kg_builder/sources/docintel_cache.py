"""Immutable local cache of complete Document Intelligence layout responses."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from pydantic import field_serializer, field_validator, model_validator

from fabric_kg_builder.contracts.base import (
    ContractModel,
    Sha256,
    canonical_json,
    canonical_sha256,
    freeze_json,
    normalize_nfc,
    reject_secret_text,
    thaw_json,
)
from fabric_kg_builder.release.redact import redact_secret_text
from fabric_kg_builder.sources.docintel_normalizer import normalize_di_layout


class DocIntelCacheError(ValueError):
    """Invalid, conflicting, or unsupported cached layout; never a cache miss."""


class UnsupportedLayoutCacheVersion(DocIntelCacheError):
    """A preserved cache entry needs an explicit format migration decision."""


def _json_value(value: Any, *, require_nfc: bool = False) -> None:
    if type(value) is str:
        if require_nfc and normalize_nfc(value) != value:
            raise DocIntelCacheError("extractor identity strings must already be NFC")
        return
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DocIntelCacheError("layout JSON object keys must be strings")
            _json_value(key, require_nfc=require_nfc)
            _json_value(item, require_nfc=require_nfc)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _json_value(item, require_nfc=require_nfc)
        return
    raise DocIntelCacheError("layout cache requires finite JSON values")


def _secret_field(name: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", name.casefold())
    words = re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower())
    return (
        "key" in words
        or "password" in compact
        or "secret" in compact
        or "token" in compact
        or "credential" in compact
        or compact.endswith("key")
        or compact in {"authorization", "connectionstring", "sig", "sas"}
    )


def _identity_is_nonsecret(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _secret_field(key):
                raise DocIntelCacheError("extractor identity must not contain credential fields")
            _identity_is_nonsecret(key)
            _identity_is_nonsecret(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _identity_is_nonsecret(item)
    elif isinstance(value, str):
        reject_secret_text(value, field_name="extractor_identity")
        if redact_secret_text(value) != value:
            raise DocIntelCacheError("extractor identity must not contain secret values")
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or any(
            _secret_field(key) or key.casefold() in {"se", "sp", "sv", "spr", "st"}
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        ):
            raise DocIntelCacheError("extractor identity must not contain credential URLs")


def validate_extractor_identity(identity: dict[str, Any]) -> None:
    """Reject invalid or secret-bearing configuration without cache I/O."""
    if not isinstance(identity, dict) or not identity:
        raise DocIntelCacheError("extractor identity must be a nonempty JSON object")
    _json_value(identity, require_nfc=True)
    try:
        _identity_is_nonsecret(identity)
    except ValueError as exc:
        raise DocIntelCacheError("extractor identity must not contain credentials") from exc


def _lossless_result_json(raw: Any) -> str:
    if not isinstance(raw, dict) or not isinstance(raw.get("content"), str):
        raise DocIntelCacheError("raw layout must be an object containing a content string")
    _json_value(raw)
    return json.dumps(
        raw, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class CachedLayout(ContractModel):
    """A complete raw result bound to source bytes and caller-supplied configuration.

    The caller supplies endpoint identity, API version, model and all analysis
    options in ``extractor_identity``. Authentication material never belongs there.
    The ASCII-escaped raw JSON string preserves every original Unicode codepoint,
    including non-NFC keys and text, outside canonical contract normalization.
    """

    contract_version: Literal["1.1.0"] = "1.1.0"
    input_sha256: Sha256
    extractor_identity: dict[str, Any]
    analyze_result_json: str
    response_hash: Sha256
    cache_key: Sha256

    @field_validator("extractor_identity", mode="before")
    @classmethod
    def _require_json_object(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise DocIntelCacheError("cached layout fields must be JSON objects")
        _json_value(value, require_nfc=True)
        return value

    @field_validator("extractor_identity")
    @classmethod
    def _identity(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_extractor_identity(value)
        return freeze_json(value)

    @field_validator("analyze_result_json")
    @classmethod
    def _result(cls, value: str) -> str:
        if _lossless_result_json(json.loads(value)) != value:
            raise DocIntelCacheError("raw layout JSON must use the lossless 1.1 encoding")
        return value

    @field_serializer("extractor_identity")
    def _serialize_json(self, value: dict[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    @property
    def analyze_result(self) -> dict[str, Any]:
        """Return a deeply immutable decoded view without normalizing raw text."""
        return freeze_json(json.loads(self.analyze_result_json))

    @model_validator(mode="after")
    def _hashes(self) -> "CachedLayout":
        if self.response_hash != hashlib.sha256(self.analyze_result_json.encode("ascii")).hexdigest():
            raise DocIntelCacheError("cached layout response hash differs")
        if self.cache_key != canonical_sha256({
            "input_sha256": self.input_sha256,
            "extractor_identity": self.extractor_identity,
        }):
            raise DocIntelCacheError("cached layout input/configuration key differs")
        return self


def make_cached_layout(
    data: bytes, result: Any, extractor_identity: dict[str, Any]
) -> CachedLayout:
    """Seal a raw dict or SDK ``as_dict()`` result, without calling any service."""
    try:
        if not isinstance(data, bytes):
            raise DocIntelCacheError("layout input must be raw bytes")
        raw = result if isinstance(result, dict) else result.as_dict()
        raw_json = _lossless_result_json(raw)
        validate_extractor_identity(extractor_identity)
        input_sha256 = hashlib.sha256(data).hexdigest()
        return CachedLayout(
            input_sha256=input_sha256,
            extractor_identity=extractor_identity,
            analyze_result_json=raw_json,
            response_hash=hashlib.sha256(raw_json.encode("ascii")).hexdigest(),
            cache_key=canonical_sha256({
                "input_sha256": input_sha256,
                "extractor_identity": extractor_identity,
            }),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise DocIntelCacheError("cannot seal invalid or secret-bearing raw layout") from exc


def _cache_key(input_sha256: str, extractor_identity: dict[str, Any]) -> str:
    if not isinstance(input_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", input_sha256) is None:
        raise DocIntelCacheError("input_sha256 must be the SHA-256 of raw file bytes")
    validate_extractor_identity(extractor_identity)
    return canonical_sha256({
        "input_sha256": input_sha256,
        "extractor_identity": extractor_identity,
    })


def load_cached_layout(
    cache_dir: Path | str, *, input_sha256: str, extractor_identity: dict[str, Any]
) -> CachedLayout | None:
    """Read exactly one content/configuration key; corruption is never a miss."""
    key = _cache_key(input_sha256, extractor_identity)
    path = Path(cache_dir) / f"{key}.json"
    try:
        if path.is_symlink():
            raise DocIntelCacheError("layout cache entries must not be symbolic links")
        encoded = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DocIntelCacheError("cannot read immutable layout cache entry") from exc
    try:
        payload = json.loads(encoded)
        if isinstance(payload, dict) and payload.get("contract_version") != "1.1.0":
            raise UnsupportedLayoutCacheVersion(
                "layout cache format 1.1.0 is required; existing bytes are preserved "
                "without automatic migration or reanalysis"
            )
        record = CachedLayout.model_validate_json(encoded)
        if record.cache_key != key or (
            canonical_json(record) + "\n"
        ).encode("utf-8") != encoded:
            raise DocIntelCacheError("cached layout key or canonical bytes differ")
        return record
    except UnsupportedLayoutCacheVersion:
        raise
    except (TypeError, ValueError) as exc:
        raise DocIntelCacheError("cached layout is invalid; refusing to reanalyze implicitly") from exc


def store_cached_layout(cache_dir: Path | str, record: CachedLayout) -> Path:
    """Atomically publish a create-only entry; an existing different result fails."""
    try:
        record = CachedLayout.model_validate(record.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise DocIntelCacheError("cannot store an invalid cached layout") from exc
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{record.cache_key}.json"
    pending = root / f".{record.cache_key}.{uuid4().hex}.pending"
    encoded = (canonical_json(record) + "\n").encode("utf-8")
    pending_created = False
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        pending_created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(pending, path)
        except FileExistsError:
            existing = load_cached_layout(
                root,
                input_sha256=record.input_sha256,
                extractor_identity=thaw_json(record.extractor_identity),
            )
            if existing != record:
                raise DocIntelCacheError("immutable layout cache already contains a different response")
    finally:
        if pending_created:
            pending.unlink(missing_ok=True)
    return path


def _span_boundaries(content: str, index_type: str | None) -> dict[int, int]:
    if index_type == "unicodeCodePoint":
        return {index: index for index in range(len(content) + 1)}
    if index_type == "utf16CodeUnit":
        boundaries = {0: 0}
        offset = 0
        for index, character in enumerate(content, start=1):
            offset += 2 if ord(character) > 0xFFFF else 1
            boundaries[offset] = index
        return boundaries
    if index_type in (None, "textElements") and content.isascii() and "\r\n" not in content:
        return {index: index for index in range(len(content) + 1)}
    raise DocIntelCacheError("page span string-index convention is unsupported or ambiguous")


def layout_text_pages(record: CachedLayout) -> tuple[tuple[int | None, str], ...]:
    """Extract only verified page-span text, never synthetic page attribution."""
    try:
        record = CachedLayout.model_validate(record.model_dump(mode="python"))
        raw = thaw_json(record.analyze_result)
        content = raw["content"]
        pages = raw.get("pages", [])
        if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
            raise DocIntelCacheError("raw layout pages must be objects")
        if any(page.get("spans") is not None and not isinstance(page["spans"], list) for page in pages):
            raise DocIntelCacheError("raw layout page spans must be arrays")
        if not pages or all(not page.get("spans") for page in pages):
            normalized = normalize_di_layout({"content": content})
            return ((None, normalized.raw_content),) if normalized.raw_content else ()
        boundaries = _span_boundaries(
            content, raw.get("stringIndexType", raw.get("string_index_type"))
        )
        verified_pages = []
        page_numbers: set[int] = set()
        intervals: list[tuple[int, int]] = []
        for page in pages:
            number = page.get("pageNumber", page.get("page_number"))
            spans = page.get("spans")
            if (
                type(number) is not int or number < 1 or number in page_numbers
                or not isinstance(spans, list) or not spans
            ):
                raise DocIntelCacheError("page numbers or span coverage are invalid")
            page_numbers.add(number)
            verified_spans = []
            for span in spans:
                if not isinstance(span, dict):
                    raise DocIntelCacheError("page spans must be objects")
                offset, length = span.get("offset"), span.get("length")
                if type(offset) is not int or type(length) is not int or offset < 0 or length < 0:
                    raise DocIntelCacheError("page span offsets must be nonnegative integers")
                end = offset + length
                if offset not in boundaries or end not in boundaries:
                    raise DocIntelCacheError("page span lies outside verified content boundaries")
                start_char, end_char = boundaries[offset], boundaries[end]
                if end_char > start_char:
                    intervals.append((start_char, end_char))
                verified_spans.append({"offset": start_char, "length": end_char - start_char})
            if verified_spans != sorted(verified_spans, key=lambda item: item["offset"]):
                raise DocIntelCacheError("page spans must be ordered")
            verified_pages.append({"page_number": number, "spans": verified_spans})
        ordered = sorted(intervals)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            raise DocIntelCacheError("page spans must not overlap")
        normalized = normalize_di_layout({"content": content, "pages": verified_pages})
        return tuple(
            (page.page_number, "".join(span.text for span in page.spans))
            for page in normalized.pages
            if any(span.text for span in page.spans)
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise DocIntelCacheError("cannot derive verified text pages from cached layout") from exc
