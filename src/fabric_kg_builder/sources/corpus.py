"""Complete L1 corpus inventory and bounded design-sample manifests."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Callable, Iterator, Literal

from pydantic import Field, field_validator, model_validator

from fabric_kg_builder.contracts.base import (
    ContractModel,
    RequiredText,
    Sha256,
    canonical_sha256,
    deterministic_contract_id,
    sorted_unique,
)
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope
from fabric_kg_builder.model.ids import make_source_file_id

from .adapter import MAX_FILE_BYTES, AdapterError, FailureType
from .router import route

NonNegativeInt = Annotated[int, Field(ge=0)]
_SNAPSHOT_CHUNK_BYTES = 1024 * 1024


class SourceCorpusEntry(ContractModel):
    source_file_id: RequiredText
    asset_id: RequiredText
    asset_version_id: RequiredText
    original_byte_hash: Sha256
    byte_count: NonNegativeInt
    media_type: RequiredText
    relative_source_ref: RequiredText
    disposition: Literal["eligible", "excluded", "blocked"]
    adapter_status: Literal[
        "supported", "unsupported", "unreadable", "invalid_media", "failed"
    ]
    adapter_name: RequiredText | None = None
    reason_code: RequiredText | None = None

    @model_validator(mode="after")
    def _disposition_fields(self) -> "SourceCorpusEntry":
        if self.disposition == "eligible":
            if self.adapter_status != "supported" or self.adapter_name is None:
                raise ValueError("eligible entries require a supported adapter")
            if self.reason_code is not None:
                raise ValueError("eligible entries cannot contain a reason code")
        elif self.reason_code is None:
            raise ValueError("excluded or blocked entries require a reason code")
        if Path(self.relative_source_ref).is_absolute() or ".." in Path(
            self.relative_source_ref
        ).parts:
            raise ValueError("relative_source_ref must be a safe relative path")
        return self


class SourceCorpusManifest(ContractModel):
    identity: CanonicalIdentityEnvelope
    contract_version: Literal["1.0.0"] = "1.0.0"
    source_corpus_manifest_id: RequiredText
    inventory_scope: Literal["complete"] = "complete"
    corpus_root_id: RequiredText
    corpus_root_hash: Sha256
    entries: tuple[SourceCorpusEntry, ...]
    total_entry_count: NonNegativeInt
    eligible_entry_count: NonNegativeInt
    excluded_entry_count: NonNegativeInt
    blocked_entry_count: NonNegativeInt
    total_byte_count: NonNegativeInt
    corpus_hash: Sha256

    @field_validator("entries", mode="before")
    @classmethod
    def _sort_entries(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.source_file_id
                        if isinstance(item, SourceCorpusEntry)
                        else str(item.get("source_file_id", ""))
                    ),
                )
            )
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "SourceCorpusManifest":
        if self.identity.contract_kind != "l1.source_corpus_manifest":
            raise ValueError(
                "identity.contract_kind must be l1.source_corpus_manifest"
            )
        source_file_ids = [entry.source_file_id for entry in self.entries]
        relative_refs = [entry.relative_source_ref for entry in self.entries]
        if len(source_file_ids) != len(set(source_file_ids)):
            raise ValueError("source corpus source_file_ids must be unique")
        if len(relative_refs) != len(set(relative_refs)):
            raise ValueError("source corpus relative refs must be unique")
        if source_file_ids != sorted(source_file_ids):
            raise ValueError("source corpus entries must be sorted by source_file_id")
        counts = {
            disposition: sum(
                entry.disposition == disposition for entry in self.entries
            )
            for disposition in ("eligible", "excluded", "blocked")
        }
        if self.total_entry_count != len(self.entries):
            raise ValueError("total_entry_count must equal complete entry count")
        if self.eligible_entry_count != counts["eligible"]:
            raise ValueError("eligible_entry_count mismatch")
        if self.excluded_entry_count != counts["excluded"]:
            raise ValueError("excluded_entry_count mismatch")
        if self.blocked_entry_count != counts["blocked"]:
            raise ValueError("blocked_entry_count mismatch")
        if self.total_byte_count != sum(entry.byte_count for entry in self.entries):
            raise ValueError("total_byte_count mismatch")
        expected_root_hash = canonical_sha256({"corpus_root_id": self.corpus_root_id})
        if self.corpus_root_hash != expected_root_hash:
            raise ValueError("corpus_root_hash does not match corpus_root_id")
        semantic_values = self.model_dump(
            mode="json",
            exclude={
                "identity",
                "source_corpus_manifest_id",
                "corpus_hash",
            },
        )
        expected_hash = canonical_sha256(semantic_values)
        if self.corpus_hash != expected_hash:
            raise ValueError("corpus_hash does not match complete corpus inventory")
        expected_id = deterministic_contract_id(
            "source-corpus-manifest", {"corpus_hash": self.corpus_hash}
        )
        if self.source_corpus_manifest_id != expected_id:
            raise ValueError(
                "source_corpus_manifest_id does not match deterministic seed"
            )
        if self.identity.content_hash != self.corpus_hash:
            raise ValueError("identity.content_hash must equal corpus_hash")
        return self


@dataclass(frozen=True)
class SourceAssetSnapshot:
    """Private snapshot whose held descriptor is the extraction authority."""

    entry: SourceCorpusEntry
    path: Path
    adapter_name: str
    byte_hash: str
    byte_count: int
    device: int
    inode: int
    _descriptor: BinaryIO


@dataclass(frozen=True)
class VerifiedSourceExtraction:
    """Adapter output bound to the exact guarded snapshot consumed."""

    adapter_result: Any
    source_file_id: str
    asset_version_id: str
    adapter_name: str
    consumed_byte_hash: str
    consumed_byte_count: int


class SourceSnapshotIntegrityError(AdapterError):
    """Typed failure raised only when sealed snapshot authority is violated."""


def _snapshot_error(snapshot: SourceAssetSnapshot, message: str) -> AdapterError:
    return SourceSnapshotIntegrityError(
        FailureType.CORRUPT,
        f"{message} for {snapshot.entry.source_file_id}",
        source_locator=snapshot.entry.relative_source_ref,
    )


def _write_all(stream: BinaryIO, chunk: bytes) -> None:
    remaining = memoryview(chunk)
    while remaining:
        written = stream.write(remaining)
        if written is None or written <= 0:
            raise OSError("private snapshot write made no progress")
        remaining = remaining[written:]


def _hash_open_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    stream.seek(0)
    while chunk := stream.read(_SNAPSHOT_CHUNK_BYTES):
        digest.update(chunk)
        byte_count += len(chunk)
    stream.seek(0)
    return digest.hexdigest(), byte_count


def _verify_snapshot(snapshot: SourceAssetSnapshot) -> None:
    try:
        descriptor_stat = os.fstat(snapshot._descriptor.fileno())
        path_stat = snapshot.path.stat(follow_symlinks=False)
    except OSError as exc:
        raise _snapshot_error(snapshot, "private snapshot became unavailable") from exc
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or descriptor_stat.st_dev != snapshot.device
        or descriptor_stat.st_ino != snapshot.inode
        or path_stat.st_dev != snapshot.device
        or path_stat.st_ino != snapshot.inode
    ):
        raise _snapshot_error(snapshot, "private snapshot path or inode changed")
    byte_hash, byte_count = _hash_open_stream(snapshot._descriptor)
    if (
        byte_hash != snapshot.byte_hash
        or byte_count != snapshot.byte_count
        or byte_hash != snapshot.entry.original_byte_hash
        or byte_count != snapshot.entry.byte_count
    ):
        raise _snapshot_error(snapshot, "private snapshot bytes changed")


def _remove_private_snapshot(temp_root: Path) -> None:
    def make_writable_and_retry(
        function: Callable[[str], None],
        path: str,
        _: tuple[type[BaseException], BaseException, object],
    ) -> None:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(path)

    shutil.rmtree(temp_root, onerror=make_writable_and_retry)


@contextmanager
def open_verified_source_snapshot(
    path: Path,
    *,
    entry: SourceCorpusEntry,
    corpus_root_id: str | None = None,
    max_bytes: int = MAX_FILE_BYTES,
    _read_hook: Callable[[int], None] | None = None,
) -> Iterator[SourceAssetSnapshot]:
    """Stream source bytes into one bounded private snapshot and hold its inode."""

    if entry.disposition != "eligible" or entry.adapter_status != "supported":
        raise SourceSnapshotIntegrityError(
            FailureType.CORRUPT,
            f"source entry {entry.source_file_id} is not eligible for extraction",
            source_locator=entry.relative_source_ref,
        )
    if entry.adapter_name is None:
        raise SourceSnapshotIntegrityError(
            FailureType.CORRUPT,
            f"source entry {entry.source_file_id} has no sealed adapter kind",
            source_locator=entry.relative_source_ref,
        )
    if entry.byte_count > max_bytes:
        raise SourceSnapshotIntegrityError(
            FailureType.TOO_LARGE,
            f"source entry {entry.source_file_id} exceeds the byte snapshot limit",
            source_locator=entry.relative_source_ref,
        )

    temp_root = Path(tempfile.mkdtemp(prefix="fabric-kg-source-"))
    os.chmod(temp_root, 0o700)
    snapshot_path = temp_root / Path(entry.relative_source_ref).name
    descriptor: BinaryIO | None = None
    try:
        descriptor = snapshot_path.open("xb+", buffering=0)
        os.chmod(snapshot_path, 0o600)
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with path.open("rb", buffering=0) as source_stream:
                while chunk := source_stream.read(_SNAPSHOT_CHUNK_BYTES):
                    byte_count += len(chunk)
                    if byte_count > entry.byte_count or byte_count > max_bytes:
                        raise SourceSnapshotIntegrityError(
                            FailureType.CORRUPT,
                            f"source bytes changed for {entry.source_file_id}",
                            source_locator=entry.relative_source_ref,
                        )
                    digest.update(chunk)
                    _write_all(descriptor, chunk)
                    if _read_hook is not None:
                        _read_hook(byte_count)
        except AdapterError:
            raise
        except OSError as exc:
            raise SourceSnapshotIntegrityError(
                FailureType.NOT_FOUND,
                f"source bytes are unavailable for {entry.source_file_id}",
                source_locator=entry.relative_source_ref,
            ) from exc

        descriptor.flush()
        os.fsync(descriptor.fileno())
        os.chmod(snapshot_path, 0o400)
        original_byte_hash = digest.hexdigest()
        expected_asset_id = (
            deterministic_contract_id(
                "asset",
                {
                    "corpus_root_id": corpus_root_id,
                    "relative_source_ref": entry.relative_source_ref,
                },
            )
            if corpus_root_id is not None
            else entry.asset_id
        )
        expected_source_file_id = make_source_file_id(
            entry.relative_source_ref,
            original_byte_hash,
        )
        expected_asset_version_id = deterministic_contract_id(
            "asset-version",
            {
                "asset_id": expected_asset_id,
                "original_byte_hash": original_byte_hash,
            },
        )
        expected_media_type = (
            mimetypes.guess_type(Path(entry.relative_source_ref).name)[0]
            or "application/octet-stream"
        )
        if (
            byte_count != entry.byte_count
            or original_byte_hash != entry.original_byte_hash
            or expected_source_file_id != entry.source_file_id
            or expected_asset_id != entry.asset_id
            or expected_asset_version_id != entry.asset_version_id
            or expected_media_type != entry.media_type
        ):
            raise SourceSnapshotIntegrityError(
                FailureType.CORRUPT,
                f"source bytes or sealed identity changed for {entry.source_file_id}",
                source_locator=entry.relative_source_ref,
            )
        descriptor_stat = os.fstat(descriptor.fileno())
        snapshot = SourceAssetSnapshot(
            entry=entry,
            path=snapshot_path,
            adapter_name=entry.adapter_name,
            byte_hash=original_byte_hash,
            byte_count=byte_count,
            device=descriptor_stat.st_dev,
            inode=descriptor_stat.st_ino,
            _descriptor=descriptor,
        )
        yield snapshot
    finally:
        if descriptor is not None:
            descriptor.close()
        _remove_private_snapshot(temp_root)


def extract_verified_source_snapshot(
    snapshot: SourceAssetSnapshot,
    *,
    _consume_hook: Callable[[str, SourceAssetSnapshot], None] | None = None,
) -> VerifiedSourceExtraction:
    """Run a path adapter inside descriptor/inode and pre/post hash guards.

    Existing adapters are trusted to parse the guarded path. The wrapper does
    not claim that they consume every byte; it guarantees that the private inode
    they could read equals the sealed snapshot before and after adapter use.
    """

    from .router import extract_with_adapter

    _verify_snapshot(snapshot)
    if _consume_hook is not None:
        _consume_hook("before_route", snapshot)
    actual_adapter = route(snapshot.path)
    if actual_adapter != snapshot.adapter_name:
        raise AdapterError(
            FailureType.MIME_MISMATCH,
            f"sealed adapter kind changed for {snapshot.entry.source_file_id}",
            source_locator=snapshot.entry.relative_source_ref,
        )
    if _consume_hook is not None:
        _consume_hook("before_adapter", snapshot)
    _verify_snapshot(snapshot)
    result = extract_with_adapter(snapshot.path, snapshot.adapter_name)
    if _consume_hook is not None:
        _consume_hook("after_adapter", snapshot)
    _verify_snapshot(snapshot)
    return VerifiedSourceExtraction(
        adapter_result=result,
        source_file_id=snapshot.entry.source_file_id,
        asset_version_id=snapshot.entry.asset_version_id,
        adapter_name=snapshot.adapter_name,
        consumed_byte_hash=snapshot.byte_hash,
        consumed_byte_count=snapshot.byte_count,
    )


class DesignSampleEntry(ContractModel):
    source_file_id: RequiredText
    source_unit_ids: tuple[RequiredText, ...]
    evidence_span_ids: tuple[RequiredText, ...]
    sample_kind: Literal["heading", "text", "table", "visual_description"]
    sample_order: NonNegativeInt

    @field_validator("source_unit_ids", "evidence_span_ids", mode="before")
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @model_validator(mode="after")
    def _sample_artifacts(self) -> "DesignSampleEntry":
        if not self.source_unit_ids or not self.evidence_span_ids:
            raise ValueError("design samples require SourceUnits and EvidenceSpans")
        return self


class DesignSampleManifest(ContractModel):
    identity: CanonicalIdentityEnvelope
    contract_version: Literal["1.0.0"] = "1.0.0"
    design_sample_manifest_id: RequiredText
    sample_scope: Literal["bounded_domain_design"] = "bounded_domain_design"
    source_corpus_manifest_id: RequiredText
    source_corpus_manifest_hash: Sha256
    budget_snapshot_hash: Sha256
    entries: tuple[DesignSampleEntry, ...]
    completeness_disclaimer: Literal[
        "bounded design samples are not the complete source universe and cannot satisfy L2 extraction evidence"
    ] = "bounded design samples are not the complete source universe and cannot satisfy L2 extraction evidence"
    sample_hash: Sha256

    @field_validator("entries", mode="before")
    @classmethod
    def _sort_entries(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.sample_order
                        if isinstance(item, DesignSampleEntry)
                        else int(item.get("sample_order", -1))
                    ),
                )
            )
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "DesignSampleManifest":
        if self.identity.contract_kind != "l1.design_sample_manifest":
            raise ValueError(
                "identity.contract_kind must be l1.design_sample_manifest"
            )
        orders = [entry.sample_order for entry in self.entries]
        if orders != list(range(len(self.entries))):
            raise ValueError("sample_order must be contiguous from zero")
        source_unit_ids = [
            source_unit_id
            for entry in self.entries
            for source_unit_id in entry.source_unit_ids
        ]
        evidence_ids = [
            evidence_id
            for entry in self.entries
            for evidence_id in entry.evidence_span_ids
        ]
        if len(source_unit_ids) != len(set(source_unit_ids)):
            raise ValueError("design SourceUnit IDs must be unique")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("design EvidenceSpan IDs must be unique")
        semantic_values = self.model_dump(
            mode="json",
            exclude={
                "identity",
                "design_sample_manifest_id",
                "sample_hash",
            },
        )
        expected_hash = canonical_sha256(semantic_values)
        if self.sample_hash != expected_hash:
            raise ValueError("sample_hash does not match bounded design sample")
        expected_id = deterministic_contract_id(
            "design-sample-manifest", {"sample_hash": self.sample_hash}
        )
        if self.design_sample_manifest_id != expected_id:
            raise ValueError(
                "design_sample_manifest_id does not match deterministic seed"
            )
        if self.identity.content_hash != self.sample_hash:
            raise ValueError("identity.content_hash must equal sample_hash")
        return self

    def validate_subset_of(self, corpus: SourceCorpusManifest) -> None:
        if self.source_corpus_manifest_id != corpus.source_corpus_manifest_id:
            raise ValueError("design sample references a different corpus manifest")
        if self.source_corpus_manifest_hash != corpus.corpus_hash:
            raise ValueError("design sample corpus hash mismatch")
        corpus_ids = {entry.source_file_id for entry in corpus.entries}
        sample_ids = {entry.source_file_id for entry in self.entries}
        missing = sample_ids - corpus_ids
        if missing:
            raise ValueError(
                f"design sample contains sources absent from corpus: {sorted(missing)}"
            )


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def collect_corpus_files(source_path: Path) -> list[tuple[Path, str]]:
    """Enumerate every regular source once, excluding generated L1 runtime state."""
    source_path = source_path.resolve()
    if source_path.is_file():
        return [(source_path, source_path.name)]
    if not source_path.is_dir():
        raise FileNotFoundError(source_path)
    files: list[tuple[Path, str]] = []
    for candidate in source_path.rglob("*"):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(source_path)
        if relative.parts and relative.parts[0] == ".fkg":
            continue
        files.append((candidate, relative.as_posix()))
    return sorted(files, key=lambda item: item[1])


def _entry_for_file(
    path: Path,
    relative_ref: str,
    *,
    corpus_root_id: str,
) -> SourceCorpusEntry:
    try:
        original_hash, byte_count = _sha256_file(path)
    except OSError as exc:
        empty_hash = hashlib.sha256(b"").hexdigest()
        source_file_id = make_source_file_id(relative_ref, empty_hash)
        return SourceCorpusEntry(
            source_file_id=source_file_id,
            asset_id=deterministic_contract_id(
                "asset",
                {"corpus_root_id": corpus_root_id, "relative_source_ref": relative_ref},
            ),
            asset_version_id=deterministic_contract_id(
                "asset-version",
                {"source_file_id": source_file_id, "original_byte_hash": empty_hash},
            ),
            original_byte_hash=empty_hash,
            byte_count=0,
            media_type="application/octet-stream",
            relative_source_ref=relative_ref,
            disposition="blocked",
            adapter_status="unreadable",
            adapter_name=None,
            reason_code=f"unreadable:{type(exc).__name__}",
        )

    source_file_id = make_source_file_id(relative_ref, original_hash)
    asset_id = deterministic_contract_id(
        "asset",
        {"corpus_root_id": corpus_root_id, "relative_source_ref": relative_ref},
    )
    asset_version_id = deterministic_contract_id(
        "asset-version",
        {"asset_id": asset_id, "original_byte_hash": original_hash},
    )
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        adapter_name = route(path)
    except AdapterError as exc:
        return SourceCorpusEntry(
            source_file_id=source_file_id,
            asset_id=asset_id,
            asset_version_id=asset_version_id,
            original_byte_hash=original_hash,
            byte_count=byte_count,
            media_type=media_type,
            relative_source_ref=relative_ref,
            disposition="blocked",
            adapter_status="invalid_media",
            adapter_name=None,
            reason_code=f"adapter:{exc.failure_type.value}",
        )
    except ValueError:
        return SourceCorpusEntry(
            source_file_id=source_file_id,
            asset_id=asset_id,
            asset_version_id=asset_version_id,
            original_byte_hash=original_hash,
            byte_count=byte_count,
            media_type=media_type,
            relative_source_ref=relative_ref,
            disposition="excluded",
            adapter_status="unsupported",
            adapter_name=None,
            reason_code="unsupported_media_type",
        )
    return SourceCorpusEntry(
        source_file_id=source_file_id,
        asset_id=asset_id,
        asset_version_id=asset_version_id,
        original_byte_hash=original_hash,
        byte_count=byte_count,
        media_type=media_type,
        relative_source_ref=relative_ref,
        disposition="eligible",
        adapter_status="supported",
        adapter_name=adapter_name,
        reason_code=None,
    )


def build_source_corpus_manifest(
    source_path: Path,
    *,
    corpus_root_id: str,
    identity: CanonicalIdentityEnvelope,
) -> SourceCorpusManifest:
    entries = tuple(
        sorted(
            (
                _entry_for_file(
                    path,
                    relative_ref,
                    corpus_root_id=corpus_root_id,
                )
                for path, relative_ref in collect_corpus_files(source_path)
            ),
            key=lambda entry: entry.source_file_id,
        )
    )
    values = {
        "contract_version": "1.0.0",
        "inventory_scope": "complete",
        "corpus_root_id": corpus_root_id,
        "corpus_root_hash": canonical_sha256({"corpus_root_id": corpus_root_id}),
        "entries": entries,
        "total_entry_count": len(entries),
        "eligible_entry_count": sum(
            entry.disposition == "eligible" for entry in entries
        ),
        "excluded_entry_count": sum(
            entry.disposition == "excluded" for entry in entries
        ),
        "blocked_entry_count": sum(
            entry.disposition == "blocked" for entry in entries
        ),
        "total_byte_count": sum(entry.byte_count for entry in entries),
    }
    corpus_hash = canonical_sha256(values)
    manifest_id = deterministic_contract_id(
        "source-corpus-manifest", {"corpus_hash": corpus_hash}
    )
    sealed_identity = identity.model_copy(
        update={
            "contract_kind": "l1.source_corpus_manifest",
            "content_hash": corpus_hash,
        }
    )
    return SourceCorpusManifest(
        identity=sealed_identity,
        source_corpus_manifest_id=manifest_id,
        **values,
        corpus_hash=corpus_hash,
    )


def validate_corpus_manifest_against_source(
    manifest: SourceCorpusManifest,
    source_path: Path,
    *,
    identity: CanonicalIdentityEnvelope,
) -> None:
    rescanned = build_source_corpus_manifest(
        source_path,
        corpus_root_id=manifest.corpus_root_id,
        identity=identity,
    )
    if rescanned.entries != manifest.entries:
        raise ValueError("source corpus entries differ from immutable manifest")
    if rescanned.corpus_hash != manifest.corpus_hash:
        raise ValueError("source corpus hash differs from immutable manifest")


def build_design_sample_manifest(
    *,
    corpus: SourceCorpusManifest,
    entries: tuple[DesignSampleEntry, ...],
    budget_snapshot_hash: str,
    identity: CanonicalIdentityEnvelope,
) -> DesignSampleManifest:
    values = {
        "contract_version": "1.0.0",
        "sample_scope": "bounded_domain_design",
        "source_corpus_manifest_id": corpus.source_corpus_manifest_id,
        "source_corpus_manifest_hash": corpus.corpus_hash,
        "budget_snapshot_hash": budget_snapshot_hash,
        "entries": entries,
        "completeness_disclaimer": (
            "bounded design samples are not the complete source universe and "
            "cannot satisfy L2 extraction evidence"
        ),
    }
    sample_hash = canonical_sha256(values)
    manifest_id = deterministic_contract_id(
        "design-sample-manifest", {"sample_hash": sample_hash}
    )
    sealed_identity = identity.model_copy(
        update={
            "contract_kind": "l1.design_sample_manifest",
            "content_hash": sample_hash,
        }
    )
    manifest = DesignSampleManifest(
        identity=sealed_identity,
        design_sample_manifest_id=manifest_id,
        **values,
        sample_hash=sample_hash,
    )
    manifest.validate_subset_of(corpus)
    return manifest
