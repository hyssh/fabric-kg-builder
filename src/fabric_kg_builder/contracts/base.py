"""Strict C0.Core contract primitives and canonical serialization."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Mapping, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "1.0.0"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SECRET_RE = re.compile(
    r"(?i)(?:authorization\s*:\s*bearer|bearer\s+[a-z0-9._~-]+|"
    r"(?:sig|token|secret|client_secret|accountkey|sharedaccesssignature)="
    r"[^&\s]+)"
)

RequiredText = Annotated[str, Field(min_length=1, pattern=r".*\S.*")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SemVer = Annotated[str, Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")]
K = TypeVar("K")
V = TypeVar("V")


class ContractError(ValueError):
    """Base error for fail-closed C0 contract validation."""


class UnknownContractKindError(ContractError):
    """Raised when a contract kind is not registered."""


class UnknownContractMajorError(ContractError):
    """Raised when a reader does not support an artifact's major version."""


class EvidencePurposeAmbiguousError(ContractError):
    """Raised when trusted context cannot prove an evidence purpose."""

    code = "C0_EVIDENCE_PURPOSE_AMBIGUOUS"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class EvidencePurposePromotionError(ContractError):
    """Raised when a legacy span is promoted beyond its proven purpose."""

    code = "C0_EVIDENCE_PURPOSE_PROMOTION_PROHIBITED"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class FrozenDict(dict[K, V]):
    """Small dependency-free immutable mapping for frozen contract fields."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("contract mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def copy(self) -> "FrozenDict[K, V]":
        return self

    def __copy__(self) -> "FrozenDict[K, V]":
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> "FrozenDict[K, V]":
        return self


class ContractModel(BaseModel):
    """Immutable strict base for all persisted C0.Core models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )

    @model_validator(mode="after")
    def _require_unicode_nfc(self) -> "ContractModel":
        """Reject non-NFC persisted strings so offsets share canonical text."""

        def check(value: Any) -> None:
            if isinstance(value, str):
                if value != normalize_nfc(value):
                    raise ValueError("contract strings must be Unicode NFC")
                return
            if isinstance(value, BaseModel):
                for item in value.__dict__.values():
                    check(item)
                return
            if isinstance(value, Mapping):
                for key, item in value.items():
                    check(key)
                    check(item)
                return
            if isinstance(value, (list, tuple)):
                for item in value:
                    check(item)

        check(self)
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Any:
        """Create a fully revalidated immutable copy.

        Pydantic's default ``model_copy(update=...)`` trusts updates without
        validation, which would permit stale self-hashes in persisted contracts.
        """
        del deep
        payload = self.model_dump(mode="python", round_trip=True)
        if update:
            payload.update(update)
        return type(self).model_validate(payload)


def normalize_nfc(value: str) -> str:
    """Return Unicode NFC without changing semantic whitespace."""
    return unicodedata.normalize("NFC", value)


def sorted_unique(values: list[str] | tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    """Normalize a set-like string collection to sorted unique NFC values."""
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} entries must be strings")
        item = normalize_nfc(value.strip())
        if not item:
            raise ValueError(f"{field_name} entries must not be empty")
        normalized.add(item)
    return tuple(sorted(normalized))


def frozen_mapping(value: Mapping[K, V]) -> FrozenDict[K, V]:
    """Defensively copy a mapping into an immutable persisted value."""
    return FrozenDict(value)


def freeze_json(value: Any) -> Any:
    """Recursively freeze an already validated JSON value."""
    if isinstance(value, dict):
        return FrozenDict({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    """Return ordinary JSON containers for serialization."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def utc_timestamp(value: datetime) -> datetime:
    """Require a timezone-aware UTC timestamp."""
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, datetime):
        checked = utc_timestamp(value)
        return checked.isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        return normalize_nfc(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON forbids non-finite numbers")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            normalized_key = normalize_nfc(key)
            if normalized_key in normalized:
                raise ValueError("Unicode normalization produced duplicate object keys")
            normalized[normalized_key] = _canonical_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize canonical UTF-8 JSON with stable Unicode and finite numbers."""
    payload = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return payload.encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def utf8_sha256(value: str) -> str:
    return hashlib.sha256(normalize_nfc(value).encode("utf-8")).hexdigest()


def hash_without(model: BaseModel, *excluded_fields: str) -> str:
    payload = model.model_dump(mode="json")
    for field in excluded_fields:
        payload.pop(field, None)
    return canonical_sha256(payload)


def deterministic_contract_id(prefix: str, value: Any) -> str:
    """Mint an ID byte-compatible with ``model.ids.make_id``."""
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return f"{prefix}:{digest[:32]}"


def reject_secret_text(value: str, *, field_name: str) -> str:
    if SECRET_RE.search(value):
        raise ValueError(f"{field_name} must not contain credentials or signed tokens")
    return value


def contract_major(version: str) -> int:
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"invalid semantic version: {version!r}")
    return int(match.group(1))
