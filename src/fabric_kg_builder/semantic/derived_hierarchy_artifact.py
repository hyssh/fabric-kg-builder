"""Writer for the W3 derived community-hierarchy artifact + receipt.

Scope note (W3, offline hierarchy integration)
-----------------------------------------------
This module turns one realized hierarchy-core outcome into the on-disk,
independently versioned derived artifact described by
``fabric_kg_builder.model.derived_hierarchy_schemas`` and receipted by
``fabric_kg_builder.contracts.derived_hierarchy_receipt.DerivedHierarchyReceipt``.

It does **not** define, construct, or own W2's hierarchy core, provider, or
result-building logic (``community_hierarchy_v2.py``) -- that remains
entirely W2's. Its main write path requires W2's own real,
``community_contracts.DerivedHierarchyOutcome`` dataclass -- not a
duck-typed stand-in, Protocol, or ``Any`` -- and validates it internally
(``assert_native_w2_outcome``, both an ``isinstance`` check and W2's own
structural validity rules) unconditionally, before any row is built or any
byte is written. This is a publishing-boundary guard enforced by the
writer itself: a caller (test or CLI) that wants to exercise this writer
must construct a real ``DerivedHierarchyOutcome`` (and real
``DerivedClusterRow``/``DerivedMembershipRow``/
``InterCommunityRelationshipDiagnostic`` rows), optionally via an
injected/mock ``CommunityProvider`` that returns W2's real result type --
never a hand-rolled shape-alike class. Injected providers do not require a
native W2 import at the *provider* layer; only the outcome object handed
to this writer must be W2's genuine type.

Every sealed-input lineage pointer (manifest id/hash, receipt id/hash,
partial-scope presence) is copied verbatim from the already-validated
``SealedL4HierarchyRows`` -- never recomputed, reinterpreted, or widened.
The hierarchy outcome's own ``status``/``reason`` are carried through
unchanged: this writer never upgrades ``empty``/``insufficient``/
``native_unavailable``/``failed`` into a bare ``complete``, and it always
persists whatever rows the outcome reports (including zero rows) so
downstream tooling sees a consistent, inspectable artifact even for empty or
sparse inputs. The sealed input's own ``partial_scope`` presence is an
orthogonal, W3-owned concept -- it is never conflated with the outcome
status; W2's vocabulary has no "partial" outcome status.

Publish ordering is validate-then-write, never interleaved: every table's
bytes, the output ``ArtifactManifest``, and the ``DerivedHierarchyReceipt``
are all built and pydantic-validated fully in memory first. Only once every
one of those constructions succeeds does this module touch disk, and it
does so for every output file before returning -- a validation failure for
any table/manifest/receipt leaves the output root completely untouched, so
a partially-written or inconsistent artifact can never be observed by a
downstream reader.

``row_hash`` is a **W3-added** content-integrity hash computed here (a
canonical SHA-256 over the row's own fields); it is not sourced from W2 and
must not be confused with any hash W2's core itself produces.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import platform
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from fabric_kg_builder.contracts.base import (
    canonical_json,
    canonical_sha256,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.derived_hierarchy_receipt import (
    DERIVED_HIERARCHY_RECEIPT_CONTRACT_VERSION,
    DerivedHierarchyReceipt,
)
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope
from fabric_kg_builder.contracts.receipts import ArtifactEntry, ArtifactManifest
from fabric_kg_builder.graph.community_contracts import (
    DerivedHierarchyOutcome,
    HierarchyStatus,
)
from fabric_kg_builder.graph.community_identity import compute_evidence_binding_hash
from fabric_kg_builder.graph.community_validation import assert_valid_derived_hierarchy
from fabric_kg_builder.model.derived_hierarchy_schemas import (
    DERIVED_HIERARCHY_ARTIFACT_CONTRACT_VERSION,
    DERIVED_HIERARCHY_CONTRACT_KINDS,
    DERIVED_HIERARCHY_TABLE_SCHEMAS,
)
from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import SealedL4HierarchyRows

# Strict import, not a hand-duplicated string literal set: derived directly
# from W2's real ``HierarchyStatus`` str-Enum so this vocabulary can never
# silently drift from W2's own definition.
_ACCEPTED_STATUSES = frozenset(status.value for status in HierarchyStatus)

#: 64 lowercase hex characters -- the only shape W2's real SHA-256
#: fingerprints/hashes ever take. Used by ``_check_lineage_and_call_context``
#: below to reject any hash-shaped field that is not either ``None`` or a
#: genuine SHA-256 hex digest (e.g. a legacy/fake 32-char placeholder).
_HEX_DIGITS = frozenset("0123456789abcdef")

#: Statuses for which W2 never populates ``execution_fingerprint`` /
#: ``input_graph_hash`` (no projection ever ran). Confirmed by reading
#: every ``DerivedHierarchyOutcome(...)`` construction site in
#: ``community_hierarchy_v2.py``: both are only set for
#: EMPTY/INSUFFICIENT/COMPLETE.
_STATUSES_WITHOUT_EXECUTION_IDENTITY = frozenset({"failed", "native_unavailable"})

_TABLE_ORDER: tuple[str, ...] = (
    "derived_community_clusters",
    "derived_community_memberships",
    "derived_inter_community_relationship_diagnostics",
)

_TABLE_FILE_NAMES: dict[str, str] = {
    "derived_community_clusters": "derived-community-clusters.parquet",
    "derived_community_memberships": "derived-community-memberships.parquet",
    "derived_inter_community_relationship_diagnostics": (
        "derived-inter-community-relationship-diagnostics.parquet"
    ),
}

_MANIFEST_FILE_NAME = "derived-hierarchy-manifest.json"
_RECEIPT_FILE_NAME = "derived-hierarchy-receipt.json"


class DerivedHierarchyAdapterError(RuntimeError):
    """Raised for any W3-writer-detected authority/shape problem.

    Never raised for a hierarchy-core-reported ``empty`` / ``insufficient``
    / ``native_unavailable`` / ``failed`` status -- those are legitimate,
    expected outcomes that get persisted and receipted, not rejected. This
    is only for inputs the writer itself cannot trust or make sense of
    (e.g. an outcome reporting a status outside the agreed vocabulary, or
    an output root that collides with the sealed input root).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def assert_native_w2_outcome(outcome: Any) -> DerivedHierarchyOutcome:
    """Hard native-type + structural-validity gate, enforced unconditionally
    by ``write_derived_hierarchy_artifact`` itself (not an optional,
    deferrable caller-side helper).

    ``write_derived_hierarchy_artifact`` calls this on every ``outcome`` it
    is handed, before building a single row or touching disk: it enforces
    both the concrete native type (``isinstance``, never shape-duck-typing
    against a ``Protocol``/``Any``) and W2's own structural validity rules
    (``community_validation.assert_valid_derived_hierarchy``) -- a
    duck-typed stand-in, however shape-correct, or a structurally-invalid
    real outcome (e.g. a ``complete`` result with an internally
    inconsistent tree), is never accepted as publishable here. A caller
    wiring in an injected/mock ``CommunityProvider`` does not itself need a
    native W2 import -- only the outcome instance it ultimately hands to
    this writer must be W2's genuine ``DerivedHierarchyOutcome``.
    """

    if not isinstance(outcome, DerivedHierarchyOutcome):
        raise DerivedHierarchyAdapterError(
            "DERIVED_HIERARCHY_OUTCOME_NOT_NATIVE",
            "expected W2's real community_contracts.DerivedHierarchyOutcome, "
            f"got {type(outcome)!r}; a duck-typed stand-in is never accepted "
            "as native here",
        )
    try:
        assert_valid_derived_hierarchy(outcome)
    except AssertionError as exc:
        raise DerivedHierarchyAdapterError(
            "DERIVED_HIERARCHY_OUTCOME_STRUCTURALLY_INVALID", str(exc)
        ) from exc
    return outcome


def _is_sha256_hex(value: Any) -> bool:
    """Strict lowercase-only sha256-hex check. ``_HEX_DIGITS`` is itself
    lowercase-only, so this must check ``value`` as-is -- never
    ``value.lower()`` -- or an uppercase/mixed-case string would silently
    pass as a valid lowercase sha256 hex digest."""
    return isinstance(value, str) and len(value) == 64 and all(ch in _HEX_DIGITS for ch in value)


def _check_lineage_and_call_context(
    outcome: DerivedHierarchyOutcome,
    sealed: SealedL4HierarchyRows,
    *,
    run_id: str,
    entity_evidence: Mapping[str, Sequence[str]],
    relationship_evidence: Mapping[str, Sequence[str]],
) -> None:
    """Typed-hash + lineage + call-context cross-reference, applied to every
    outcome regardless of native/synthetic origin.

    Independent checks, each surfaced as its own actionable error code
    rather than one generic failure:

    * every hash-shaped field this writer will persist verbatim
      (outcome-level ``execution_fingerprint``/``input_graph_hash``, and
      each cluster row's own ``execution_fingerprint``) is either ``None``
      or a genuine 64-char lowercase-hex SHA-256 digest -- never a
      legacy/placeholder shape (e.g. 32 chars);
    * every ``member_entity_ids``/``entity_id`` the outcome reports is a
      canonical entity id actually present in the sealed L4 input -- an
      outcome can never introduce an entity this writer's sealed input
      never sealed;
    * every cluster's ``domain_hash`` is consistent: either every cluster
      in the outcome carries the same single non-null value, or every
      cluster carries ``None`` -- never a mix, which would mean clusters
      from different domains silently ended up in one outcome;
    * every cluster's own ``run_id`` matches this call's ``run_id`` -- a
      cluster minted under a foreign/stale call context can never be
      published under this attempt's receipt;
    * every cluster's own ``execution_fingerprint`` matches
      ``outcome.execution_fingerprint`` whenever the outcome-level value is
      known -- a cluster carrying a different (or placeholder) fingerprint
      than the outcome it supposedly came from is rejected, not silently
      republished under the outcome's identity;
    * every membership's ``cluster_id`` references a cluster actually
      present in this same outcome -- a membership can never point at a
      cluster this outcome never reported;
    * every inter-community relationship diagnostic cross-references a
      relationship actually present in the sealed L4 input by
      ``relationship_id``, and its ``source_entity_id``/``target_entity_id``
      (exact direction, not an unordered pair) match that sealed
      relationship's recorded values exactly -- a diagnostic can never
      invent or misattribute direction for a relationship the sealed input
      never carried; its ``source_cluster_id``/``target_cluster_id`` must
      also each reference a cluster actually present in this outcome.
      (The sealed L4 relationship row -- ``semantic_asserted_relationships``
      -- has no ``relationship_type`` field of its own, unlike W2's
      ``RelationshipRow`` input contract, so that field cannot be
      cross-checked against the sealed input here; only the fields the
      sealed row actually carries are compared);
    * every key in the caller-supplied ``entity_evidence``/
      ``relationship_evidence`` sidecar mappings (the ones this writer uses
      to compute ``evidence_binding_hash`` -- see
      ``compute_evidence_binding_hash``) is a canonical entity/relationship
      id actually present in the sealed L4 input -- evidence can never be
      bound to an id the sealed input never carried;
    * for a "successful" outcome (status carries execution identity, i.e.
      not ``failed``/``native_unavailable``: ``complete``, ``empty``, or
      ``insufficient``), the set of membership ``entity_id`` values is
      exactly equal to -- not merely a subset of -- the sealed L4 input's
      entity universe. This is the specific guard against an outcome built
      from an unrelated/truncated entity set being accepted as if it fully
      covered the sealed input it is published alongside.

    This is a pure-input check: it never computes or widens a fingerprint
    itself, and it runs before any row is built, so a violation leaves the
    output root untouched (same validate-then-write guarantee as every
    other rejection path in this module).
    """

    known_entity_ids = {str(_field(row, "entity_id")) for row in sealed.entities}

    for label, value in (
        ("execution_fingerprint", outcome.execution_fingerprint),
        ("input_graph_hash", outcome.input_graph_hash),
    ):
        if value is not None and not _is_sha256_hex(value):
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_INVALID_HASH_SHAPE",
                f"outcome.{label} is not a genuine SHA-256 hex digest "
                f"(got {value!r})",
            )

    domain_hashes: set[str] = set()
    saw_null_domain_hash = False
    known_cluster_ids: set[str] = set()
    for cluster in outcome.clusters:
        cluster_id = str(_field(cluster, "cluster_id"))
        known_cluster_ids.add(cluster_id)

        fingerprint = _field(cluster, "execution_fingerprint")
        if fingerprint is not None and not _is_sha256_hex(fingerprint):
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_INVALID_HASH_SHAPE",
                f"cluster {cluster_id!r} execution_fingerprint is not a "
                f"genuine SHA-256 hex digest (got {fingerprint!r})",
            )
        if (
            outcome.execution_fingerprint is not None
            and fingerprint is not None
            and str(fingerprint) != str(outcome.execution_fingerprint)
        ):
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_CALL_CONTEXT_MISMATCH",
                f"cluster {cluster_id!r} execution_fingerprint "
                f"{fingerprint!r} does not match outcome.execution_fingerprint "
                f"{outcome.execution_fingerprint!r}",
            )

        cluster_run_id = _field(cluster, "run_id")
        if cluster_run_id is not None and str(cluster_run_id) != str(run_id):
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_CALL_CONTEXT_MISMATCH",
                f"cluster {cluster_id!r} run_id {cluster_run_id!r} does not "
                f"match this attempt's run_id {run_id!r}",
            )

        for entity_id in _field(cluster, "member_entity_ids"):
            if str(entity_id) not in known_entity_ids:
                raise DerivedHierarchyAdapterError(
                    "DERIVED_HIERARCHY_UNKNOWN_ENTITY_ID",
                    f"cluster {cluster_id!r} member_entity_ids contains "
                    f"{entity_id!r}, which is not present in the sealed L4 "
                    "input's entity rows",
                )

        domain_hash = _field(cluster, "domain_hash")
        if domain_hash is None:
            saw_null_domain_hash = True
        else:
            domain_hashes.add(str(domain_hash))

    membership_entity_ids: set[str] = set()
    for membership in outcome.memberships:
        entity_id = str(_field(membership, "entity_id"))
        if entity_id not in known_entity_ids:
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_UNKNOWN_ENTITY_ID",
                f"membership entity_id {entity_id!r} is not present in the "
                "sealed L4 input's entity rows",
            )
        membership_cluster_id = str(_field(membership, "cluster_id"))
        if membership_cluster_id not in known_cluster_ids:
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_UNKNOWN_CLUSTER_ID",
                f"membership for entity {entity_id!r} references cluster_id "
                f"{membership_cluster_id!r}, which this outcome does not "
                "report among its clusters",
            )
        membership_entity_ids.add(entity_id)

    if domain_hashes and saw_null_domain_hash:
        raise DerivedHierarchyAdapterError(
            "DERIVED_HIERARCHY_INCONSISTENT_DOMAIN_HASH",
            "clusters mix a null domain_hash with a non-null domain_hash "
            f"({sorted(domain_hashes)!r}) within the same outcome",
        )
    if len(domain_hashes) > 1:
        raise DerivedHierarchyAdapterError(
            "DERIVED_HIERARCHY_INCONSISTENT_DOMAIN_HASH",
            f"clusters carry more than one distinct domain_hash value: "
            f"{sorted(domain_hashes)!r}",
        )

    known_relationships = {
        str(_field(row, "relationship_id")): row for row in sealed.relationships
    }
    for diagnostic in outcome.inter_community_relationship_diagnostics:
        relationship_id = str(_field(diagnostic, "relationship_id"))
        sealed_relationship = known_relationships.get(relationship_id)
        if sealed_relationship is None:
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_UNKNOWN_RELATIONSHIP_ID",
                f"inter-community diagnostic references relationship_id "
                f"{relationship_id!r}, which is not present in the sealed "
                "L4 input's relationship rows",
            )
        for field_name in ("source_entity_id", "target_entity_id"):
            diagnostic_value = str(_field(diagnostic, field_name))
            sealed_value = str(_field(sealed_relationship, field_name))
            if diagnostic_value != sealed_value:
                raise DerivedHierarchyAdapterError(
                    "DERIVED_HIERARCHY_CALL_CONTEXT_MISMATCH",
                    f"inter-community diagnostic for relationship "
                    f"{relationship_id!r} reports {field_name}="
                    f"{diagnostic_value!r}, which does not match the sealed "
                    f"L4 input's recorded {field_name}={sealed_value!r}",
                )
        for endpoint_label in ("source_cluster_id", "target_cluster_id"):
            endpoint_cluster_id = str(_field(diagnostic, endpoint_label))
            if endpoint_cluster_id not in known_cluster_ids:
                raise DerivedHierarchyAdapterError(
                    "DERIVED_HIERARCHY_UNKNOWN_CLUSTER_ID",
                    f"inter-community diagnostic for relationship "
                    f"{relationship_id!r} reports {endpoint_label}="
                    f"{endpoint_cluster_id!r}, which this outcome does not "
                    "report among its clusters",
                )

    if outcome.status not in _STATUSES_WITHOUT_EXECUTION_IDENTITY:
        if membership_entity_ids != known_entity_ids:
            missing = sorted(known_entity_ids - membership_entity_ids)
            extra = sorted(membership_entity_ids - known_entity_ids)
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_ENTITY_UNIVERSE_NOT_CONSERVED",
                f"outcome status {outcome.status!r} must cover exactly the "
                "sealed L4 input's entity universe via membership rows; "
                f"missing entity_ids={missing!r}, unexpected entity_ids="
                f"{extra!r}",
            )

    for entity_id in entity_evidence:
        if str(entity_id) not in known_entity_ids:
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_UNKNOWN_ENTITY_ID",
                f"entity_evidence contains entity_id {entity_id!r}, which "
                "is not present in the sealed L4 input's entity rows",
            )
    for relationship_id in relationship_evidence:
        if str(relationship_id) not in known_relationships:
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_UNKNOWN_RELATIONSHIP_ID",
                f"relationship_evidence contains relationship_id "
                f"{relationship_id!r}, which is not present in the sealed "
                "L4 input's relationship rows",
            )


def _fsync_path(path: Path) -> None:
    """Best-effort durability: fsync a file or directory by path.

    Directories are opened read-only (``os.O_RDONLY``) since that is the
    only mode most platforms accept for fsyncing a directory entry itself
    (to make a prior create/rename durable); files are also opened
    read-only here since we only need to flush already-written contents,
    never to modify them.
    """

    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes_durably(path: Path, payload: bytes) -> None:
    """``Path.write_bytes`` plus an immediate fsync of the written file.

    Deliberately still calls ``path.write_bytes`` itself (never an
    alternate low-level write) so existing call-count-sensitive tests over
    the write path are unaffected; this only adds a durability guarantee
    on top.
    """

    path.write_bytes(payload)
    _fsync_path(path)


_LIBC: ctypes.CDLL | None = None


def _libc() -> ctypes.CDLL:
    """Lazily load the C runtime for the exclusive-rename syscalls below."""

    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL(None, use_errno=True)
    return _LIBC


def _raise_already_published_or_oserror(output_root: Path, raw_errno: int) -> None:
    if raw_errno == errno.EEXIST:
        raise DerivedHierarchyAdapterError(
            "DERIVED_HIERARCHY_OUTPUT_ROOT_ALREADY_EXISTS",
            f"output_root {output_root} already exists; this writer never "
            "overwrites an existing run -- choose a new output_root (for "
            "example one scoped by run_id) for each publish",
        )
    raise OSError(raw_errno, os.strerror(raw_errno), str(output_root))


def _renameat2_no_replace(staging_dir: Path, output_root: Path) -> None:
    """Linux: ``renameat2(..., RENAME_NOREPLACE)`` -- atomically rename
    ``staging_dir`` onto ``output_root`` but fail with ``EEXIST`` instead of
    silently replacing it if ``output_root`` already exists (created by
    anything, at any instant up to and including the syscall itself).
    """

    at_fdcwd = -100
    rename_noreplace = 0x1
    src = os.fsencode(str(staging_dir))
    dst = os.fsencode(str(output_root))
    libc = _libc()
    result = libc.renameat2(
        ctypes.c_int(at_fdcwd),
        ctypes.c_char_p(src),
        ctypes.c_int(at_fdcwd),
        ctypes.c_char_p(dst),
        ctypes.c_uint(rename_noreplace),
    )
    if result != 0:
        _raise_already_published_or_oserror(output_root, ctypes.get_errno())


def _renamex_np_excl(staging_dir: Path, output_root: Path) -> None:
    """macOS (Darwin): ``renamex_np(..., RENAME_EXCL)`` -- the Darwin
    equivalent of ``renameat2(RENAME_NOREPLACE)`` (available since macOS
    10.12); fails with ``EEXIST`` instead of replacing an existing
    ``output_root``.
    """

    rename_excl = 0x0004
    src = os.fsencode(str(staging_dir))
    dst = os.fsencode(str(output_root))
    libc = _libc()
    result = libc.renamex_np(
        ctypes.c_char_p(src), ctypes.c_char_p(dst), ctypes.c_uint(rename_excl)
    )
    if result != 0:
        _raise_already_published_or_oserror(output_root, ctypes.get_errno())


def _exclusive_publish_rename(staging_dir: Path, output_root: Path) -> None:
    """Publish ``staging_dir`` as ``output_root`` with a genuinely atomic,
    *exclusive* (never-replace) rename.

    Plain ``os.rename``/``Path.rename`` is **not** sufficient here: on
    POSIX, renaming a directory onto an existing *empty* destination
    directory silently succeeds and replaces it -- there is no error, no
    warning, nothing -- which is exactly the race this closes. If another
    writer (or an attacker, or simply a second concurrent invocation)
    creates ``output_root`` at any point up to and including the instant of
    this call, that destination must never be silently clobbered; the
    publish must fail loudly instead.

    This dispatches to a true kernel-level no-replace rename primitive --
    ``renameat2(RENAME_NOREPLACE)`` on Linux, ``renamex_np(RENAME_EXCL)`` on
    macOS -- so the existence check and the rename are one atomic kernel
    operation with no gap a second writer could win. On any other platform
    (notably Windows, where ``os.rename``/``MoveFileEx`` without
    ``MOVEFILE_REPLACE_EXISTING`` already refuses to replace an existing
    destination by default) the ordinary exclusive ``Path.rename`` is used
    directly, since it already provides the same guarantee there.
    """

    system = platform.system()
    if system == "Linux":
        _renameat2_no_replace(staging_dir, output_root)
    elif system == "Darwin":
        _renamex_np_excl(staging_dir, output_root)
    else:
        try:
            staging_dir.rename(output_root)
        except FileExistsError:
            _raise_already_published_or_oserror(output_root, errno.EEXIST)


@dataclass(frozen=True)
class DerivedHierarchyWriteResult:
    """Everything the CLI needs to report on one write, without re-reading."""

    output_root: Path
    table_paths: dict[str, Path]
    manifest_path: Path
    receipt_path: Path
    manifest: ArtifactManifest
    receipt: DerivedHierarchyReceipt


def _field(row: Any, name: str) -> Any:
    """Read ``name`` off ``row`` whether it is Mapping-like or attribute-like.

    W2's real rows are frozen dataclasses, but this bridges Mapping-like
    test doubles too rather than assuming one concrete representation.
    """

    if isinstance(row, Mapping):
        return row[name]
    return getattr(row, name)


def _finalize_row(
    fields: Mapping[str, Any], run_context: Mapping[str, Any]
) -> dict[str, Any]:
    payload = {**fields, **run_context}
    payload["row_hash"] = canonical_sha256(payload)
    return payload


def _cluster_row(row: Any, cluster_context: Mapping[str, Any]) -> dict[str, Any]:
    # Every provenance field here except ``config_id``/``seed`` is already
    # native to W2's real ``DerivedClusterRow`` -- ``run_id``, ``policy_id``,
    # and ``hierarchy_version`` are read straight off the row, not pulled
    # from the writer's own run context, so a row's own (possibly per-batch)
    # values always win over the caller-supplied run context.
    fields = {
        "cluster_id": _field(row, "cluster_id"),
        "hierarchy_version": _field(row, "hierarchy_version"),
        "parent_cluster_id": _field(row, "parent_cluster_id"),
        "structural_height": _field(row, "structural_height"),
        "native_depth": _field(row, "native_depth"),
        "is_terminal": _field(row, "is_terminal"),
        "is_singleton_isolate": _field(row, "is_singleton_isolate"),
        "is_oversized": _field(row, "is_oversized"),
        "member_entity_ids": list(_field(row, "member_entity_ids")),
        "evidence_ids": list(_field(row, "evidence_ids")),
        "relationship_contributed_evidence_ids": list(
            _field(row, "relationship_contributed_evidence_ids")
        ),
        "provider_id": _field(row, "provider_id"),
        "provider_version": _field(row, "provider_version"),
        "policy_id": _field(row, "policy_id"),
        "domain_hash": _field(row, "domain_hash"),
        "run_id": _field(row, "run_id"),
        "execution_fingerprint": _field(row, "execution_fingerprint"),
    }
    return _finalize_row(fields, cluster_context)


def _membership_row(row: Any, run_context: Mapping[str, Any]) -> dict[str, Any]:
    return _finalize_row(
        {
            "cluster_id": _field(row, "cluster_id"),
            "entity_id": _field(row, "entity_id"),
            "evidence_ids": list(_field(row, "evidence_ids")),
            "primary_membership": _field(row, "primary_membership"),
        },
        run_context,
    )


def _inter_community_diagnostics_row(
    row: Any, run_context: Mapping[str, Any]
) -> dict[str, Any]:
    return _finalize_row(
        {
            "relationship_id": _field(row, "relationship_id"),
            "relationship_type": _field(row, "relationship_type"),
            "source_entity_id": _field(row, "source_entity_id"),
            "target_entity_id": _field(row, "target_entity_id"),
            "source_cluster_id": _field(row, "source_cluster_id"),
            "target_cluster_id": _field(row, "target_cluster_id"),
            "reason": _field(row, "reason"),
        },
        run_context,
    )


def _build_rows(
    outcome: DerivedHierarchyOutcome, run_context: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    # Clusters already carry their own native run_id/policy_id/
    # hierarchy_version; only config_id/seed are missing from the row
    # itself, so only those two are merged in (never the full run_context,
    # which would let a mismatched run_id silently overwrite the row's own).
    cluster_context = {
        "config_id": run_context["config_id"],
        "seed": run_context["seed"],
    }
    return {
        "derived_community_clusters": [
            _cluster_row(row, cluster_context) for row in outcome.clusters
        ],
        "derived_community_memberships": [
            _membership_row(row, run_context) for row in outcome.memberships
        ],
        "derived_inter_community_relationship_diagnostics": [
            _inter_community_diagnostics_row(row, run_context)
            for row in outcome.inter_community_relationship_diagnostics
        ],
    }


def _parquet_bytes(rows: Sequence[Mapping[str, Any]], schema: pa.Schema) -> bytes:
    table = pa.Table.from_pylist(list(rows), schema=schema)
    sink = pa.BufferOutputStream()
    pq.write_table(
        table,
        sink,
        compression="zstd",
        version="2.6",
        use_dictionary=False,
        write_statistics=True,
    )
    return sink.getvalue().to_pybytes()


def _schema_descriptor(schema: pa.Schema) -> list[dict[str, Any]]:
    return [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in schema
    ]


def _schema_hash(schema: pa.Schema) -> str:
    return canonical_sha256(_schema_descriptor(schema))


def _artifact_entry(
    *,
    artifact_id: str,
    contract_kind: str,
    schema_hash: str,
    payload: bytes,
    row_count: int,
) -> ArtifactEntry:
    return ArtifactEntry(
        artifact_id=artifact_id,
        contract_kind=contract_kind,
        contract_version=DERIVED_HIERARCHY_ARTIFACT_CONTRACT_VERSION,
        schema_hash=schema_hash,
        content_hash=hashlib.sha256(payload).hexdigest(),
        canonical_id_set_hash=None,
        row_count=row_count,
        byte_count=len(payload),
        partition_count=1,
        media_type="application/vnd.apache.parquet",
        immutable_locator=None,
        blob_asset_ref_id=None,
    )


def _identity(
    sealed: SealedL4HierarchyRows, *, contract_kind: str
) -> CanonicalIdentityEnvelope:
    """Build an identity envelope rooted in the sealed L4 manifest's own.

    Every field not explicitly overridden is copied verbatim from
    ``sealed.manifest.identity``; nothing about the entity-extraction
    lineage (``asset_id``, ``prompt_hash``, ``extractor_name``, ...) is
    invented or guessed. ``parent_artifact_ids`` gains the sealed L4
    manifest's own id, marking it as the immediate ancestor of this new
    derived artifact.
    """

    values = sealed.manifest.identity.model_dump(mode="python", round_trip=True)
    values.update(
        {
            "contract_kind": contract_kind,
            "parent_artifact_ids": tuple(
                sorted(
                    {
                        *sealed.manifest.identity.parent_artifact_ids,
                        sealed.manifest.artifact_manifest_id,
                    }
                )
            ),
        }
    )
    return CanonicalIdentityEnvelope.model_validate(values)


def _check_output_root_disjoint(
    output_root: Path, protected_roots: Mapping[str, Path]
) -> None:
    """Reject an ``output_root`` that collides with any protected root.

    This writer must never write into, or be nested under/above, any root
    it reads authority/lineage from -- that could let a later re-run
    silently corrupt already-sealed bytes, or (for upstream roots) create
    a confusing cycle where this downstream artifact shadows an earlier
    pipeline stage's own state directory. Resolved (absolute, symlink-
    free) paths are compared so a relative-path or symlink alias cannot
    evade the check.

    ``protected_roots`` is a ``{label: path}`` mapping so a caller can
    cover every root this offline integration actually touches or reads
    from in one call -- not just the sealed L4 run root this writer
    itself holds a reference to:

    - the sealed L4 run root (``sealed.root``) -- this writer's own
      direct authority input;
    - every L3 input-manifest search root passed to
      ``load_sealed_l4_hierarchy_rows`` -- the upstream authority this
      offline integration transitively depends on;
    - the conventional local L1/L2/L3/L4 state-directory defaults
      (``.fkg/l1`` .. ``.fkg/l4``) used elsewhere in this CLI, so a
      caller who accepts this writer's own default ``output_root``
      cannot accidentally shadow another stage's state even when no
      sealed/search root happens to resolve to that path in a given
      invocation;
    - any local default associated with L5/L5a (there is no on-disk
      ``.fkg/l5`` state directory in this codebase -- L5a is a live
      publication target, not an offline stage with its own state root --
      so the only concrete L5-adjacent default to guard is the L5a
      release-plan directory convention (``build/release``) used by
      ``fabric-kg`` release tooling elsewhere).

    Every label is reported by name in the raised error so a caller can
    tell at a glance *which* protected root collided, rather than only
    being told that output_root was unsafe.
    """

    resolved_output = output_root.resolve()
    for label, protected_path in protected_roots.items():
        resolved_protected = protected_path.resolve()
        if resolved_output == resolved_protected:
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_OUTPUT_ROOT_COLLISION",
                f"output_root {output_root} must not equal the {label} "
                f"root {protected_path}",
            )
        if resolved_protected in resolved_output.parents or resolved_output in (
            *resolved_protected.parents,
        ):
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_OUTPUT_ROOT_COLLISION",
                f"output_root {output_root} must not nest with the {label} "
                f"root {protected_path}",
            )


def _check_output_root_not_already_published(output_root: Path) -> None:
    """Refuse to publish on top of an already-existing ``output_root``.

    This writer never overwrites an existing run, partially or
    otherwise -- a second invocation targeting the same ``output_root``
    must fail loudly instead of silently clobbering files from an earlier
    run. ``Path.is_symlink()`` is checked in addition to ``exists()`` so a
    symlink *alias* of an existing (or even a dangling) path is refused
    exactly like the real path would be -- a symlink cannot be used to
    evade this guard.
    """

    if output_root.is_symlink() or output_root.exists():
        raise DerivedHierarchyAdapterError(
            "DERIVED_HIERARCHY_OUTPUT_ROOT_ALREADY_EXISTS",
            f"output_root {output_root} already exists; this writer never "
            "overwrites an existing run -- choose a new output_root (for "
            "example one scoped by run_id) for each publish",
        )


def _output_manifest(
    sealed: SealedL4HierarchyRows,
    *,
    outcome: DerivedHierarchyOutcome,
    table_payloads: Mapping[str, bytes],
    row_counts: Mapping[str, int],
    run_id: str,
    config_id: str,
    policy_id: str,
    seed: int,
    max_cluster_size: int,
    requested_provider_name: str,
    requested_provider_version: str,
) -> ArtifactManifest:
    # Widened beyond run_id/table_name (bug fix): config_id/policy_id/seed
    # are all already available to the caller and all independently affect
    # what a run actually produces, so two runs sharing a run_id but
    # differing in any of these (e.g. a rerun with a corrected policy) must
    # still mint distinct artifact/manifest ids rather than colliding.
    #
    # Defect-2 fix: the attempted *request* shape (max_cluster_size,
    # requested provider name/version) and the hierarchy core's own
    # *realized* execution identity (status, provider id/version actually
    # used, hierarchy_version, execution_fingerprint, input_graph_hash) are
    # now both bound into every minted id below. Two attempts that differ
    # only in max_cluster_size (e.g. 50 vs 51) -- even sharing every other
    # field -- now mint distinct artifact/manifest ids, because the
    # realized execution-fingerprint the core itself reports for those two
    # attempts is already different; this binds that realized difference
    # into identity rather than trusting an input param alone. Execution
    # identity is never guessed/backfilled for failed/native_unavailable
    # outcomes (where W2 itself never populates it): it is carried through
    # as whatever the outcome actually reports, which is ``None`` for those
    # statuses already.
    has_execution_identity = outcome.status not in _STATUSES_WITHOUT_EXECUTION_IDENTITY
    realized_execution_fingerprint = (
        outcome.execution_fingerprint if has_execution_identity else None
    )
    realized_input_graph_hash = outcome.input_graph_hash if has_execution_identity else None
    identity_payload = {
        "run_id": run_id,
        "config_id": config_id,
        "policy_id": policy_id,
        "seed": seed,
        "max_cluster_size": max_cluster_size,
        "requested_provider_name": requested_provider_name,
        "requested_provider_version": requested_provider_version,
        "status": outcome.status,
        "realized_provider_id": outcome.provider_id,
        "realized_provider_version": outcome.provider_version,
        "realized_hierarchy_version": outcome.hierarchy_version,
        "execution_fingerprint": realized_execution_fingerprint,
        "input_graph_hash": realized_input_graph_hash,
    }
    entries = [
        _artifact_entry(
            artifact_id=deterministic_contract_id(
                "derived-hierarchy-table",
                {**identity_payload, "table_name": name},
            ),
            contract_kind=DERIVED_HIERARCHY_CONTRACT_KINDS[name],
            schema_hash=_schema_hash(DERIVED_HIERARCHY_TABLE_SCHEMAS[name]),
            payload=table_payloads[name],
            row_count=row_counts[name],
        )
        for name in _TABLE_ORDER
    ]
    ordered = tuple(sorted(entries, key=lambda item: item.artifact_id))
    values = {
        "identity": _identity(sealed, contract_kind="c0.artifact_manifest"),
        "artifact_manifest_id": deterministic_contract_id(
            "derived-hierarchy-manifest",
            {
                "sealed_l4_manifest_id": sealed.manifest.artifact_manifest_id,
                **identity_payload,
            },
        ),
        "entries": ordered,
        "total_row_count": sum(entry.row_count or 0 for entry in ordered),
        "total_byte_count": sum(entry.byte_count for entry in ordered),
    }
    return ArtifactManifest(**values, manifest_hash=canonical_sha256(values))


def _receipt(
    sealed: SealedL4HierarchyRows,
    *,
    outcome: DerivedHierarchyOutcome,
    output_manifest: ArtifactManifest,
    run_id: str,
    config_id: str,
    policy_id: str,
    seed: int,
    max_cluster_size: int,
    requested_provider_name: str,
    requested_provider_version: str,
    entity_evidence: Mapping[str, Sequence[str]],
    relationship_evidence: Mapping[str, Sequence[str]],
) -> DerivedHierarchyReceipt:
    status = outcome.status
    has_execution_identity = status not in _STATUSES_WITHOUT_EXECUTION_IDENTITY
    execution_fingerprint = outcome.execution_fingerprint if has_execution_identity else None
    input_graph_hash = outcome.input_graph_hash if has_execution_identity else None
    # Defect-4 fix: never accept a precomputed hash string from the caller
    # (that was an "arbitrary caller value" with no guarantee it was ever
    # really bound to this attempt's evidence). Instead this writer always
    # computes the real digest itself, via W2's own
    # ``compute_evidence_binding_hash`` helper, from the caller-supplied
    # evidence-sidecar mappings -- the exact same mappings
    # ``derive_community_hierarchy`` itself would be given (and which
    # ``_check_lineage_and_call_context`` has already validated only
    # reference entity/relationship ids this sealed input actually
    # carries). A failed/native_unavailable attempt never had execution
    # identity in the first place, so it gets no evidence_binding_hash
    # either, consistent with ``execution_fingerprint``/``input_graph_hash``
    # above.
    evidence_binding_hash = (
        compute_evidence_binding_hash(entity_evidence, relationship_evidence)
        if has_execution_identity
        else None
    )
    values = {
        "identity": _identity(sealed, contract_kind="w3.derived_hierarchy_receipt"),
        "receipt_contract_version": DERIVED_HIERARCHY_RECEIPT_CONTRACT_VERSION,
        "derived_hierarchy_receipt_id": deterministic_contract_id(
            "derived-hierarchy-receipt",
            {
                "sealed_l4_receipt_id": sealed.receipt.stage_receipt_id,
                "output_manifest_id": output_manifest.artifact_manifest_id,
                "run_id": run_id,
                "config_id": config_id,
                "policy_id": policy_id,
                "seed": seed,
                "max_cluster_size": max_cluster_size,
                "requested_provider_name": requested_provider_name,
                "requested_provider_version": requested_provider_version,
                "status": status,
                "execution_fingerprint": execution_fingerprint,
                "input_graph_hash": input_graph_hash,
            },
        ),
        "sealed_l4_manifest_id": sealed.manifest.artifact_manifest_id,
        "sealed_l4_manifest_hash": sealed.manifest.manifest_hash,
        "sealed_l4_receipt_id": sealed.receipt.stage_receipt_id,
        "sealed_l4_receipt_hash": sealed.receipt.receipt_hash,
        "sealed_partial_scope_present": sealed.partial_scope is not None,
        "requested_provider_name": requested_provider_name,
        "requested_provider_version": requested_provider_version,
        "run_id": run_id,
        "config_id": config_id,
        "policy_id": policy_id,
        "seed": seed,
        "max_cluster_size": max_cluster_size,
        "status": status,
        "reason": outcome.reason,
        "realized_hierarchy_version": outcome.hierarchy_version,
        "realized_provider_id": outcome.provider_id,
        "realized_provider_version": outcome.provider_version,
        "execution_fingerprint": execution_fingerprint,
        "input_graph_hash": input_graph_hash,
        "evidence_binding_hash": evidence_binding_hash,
        "output_manifest_id": output_manifest.artifact_manifest_id,
        "output_manifest_hash": output_manifest.manifest_hash,
    }
    return DerivedHierarchyReceipt(**values, receipt_hash=canonical_sha256(values))


def write_derived_hierarchy_artifact(
    *,
    sealed: SealedL4HierarchyRows,
    outcome: DerivedHierarchyOutcome,
    output_root: Path,
    run_id: str,
    config_id: str,
    policy_id: str,
    seed: int,
    max_cluster_size: int,
    requested_provider_name: str,
    requested_provider_version: str,
    entity_evidence: Mapping[str, Sequence[str]] | None = None,
    relationship_evidence: Mapping[str, Sequence[str]] | None = None,
    protected_roots: Mapping[str, Path] | None = None,
) -> DerivedHierarchyWriteResult:
    """Persist one hierarchy-core outcome as the W3 derived artifact + receipt.

    ``output_root`` must be a directory distinct from (and not nested
    with) the sealed L4 run root, plus any additional ``protected_roots``
    the caller supplies (for example L3 input-manifest search roots, or
    other local pipeline-stage state directories) -- this function never
    writes into, or mutates, any authority root it or an upstream stage
    reads from. It must also not already exist: this writer refuses to
    publish on top of an existing run rather than silently overwriting it
    (resolved/symlink-aware, so a symlink alias of an existing run is
    refused exactly like the real path). It always writes all three
    tables (even with zero rows for an empty/sparse/unavailable outcome)
    and always receipts whatever ``outcome.status``/``reason`` report
    verbatim; it never substitutes a different status or claims
    ``complete`` for a non-complete outcome.

    ``entity_evidence``/``relationship_evidence`` are the same
    entity/relationship-id -> evidence-id-sequence sidecar mappings W2's
    own ``derive_community_hierarchy`` accepts (and, when omitted there,
    defaults to each row's own evidence ids) -- the caller that drove the
    provider call is expected to pass through whatever it actually used.
    This writer never accepts a precomputed ``evidence_binding_hash``
    string: it always computes that digest itself from these mappings via
    W2's real ``compute_evidence_binding_hash`` helper, and rejects any
    mapping key that is not a canonical entity/relationship id actually
    present in the sealed L4 input. Omitting either mapping is equivalent
    to passing an empty one (e.g. for a hand-built outcome with no
    evidence sidecar at all); a ``failed``/``native_unavailable`` outcome
    never has an ``evidence_binding_hash`` regardless, matching
    ``execution_fingerprint``/``input_graph_hash``.

    Every table's bytes, the manifest, and the receipt are built and
    pydantic-validated fully in memory first. Publication is then
    genuinely atomic: every file is written into a private temporary
    staging directory that is a *sibling* of ``output_root`` (same
    filesystem, so the final publish step can be a single atomic
    ``rename``), and only a successful ``rename`` of that staging
    directory onto ``output_root`` makes the run visible. Any failure
    while staging -- or the final existing-run race check immediately
    before the rename -- leaves ``output_root`` completely untouched and
    guarantees the temporary staging directory is removed; no partially
    written run, and no mutation of any other existing run, can ever be
    observed.

    Raises ``DerivedHierarchyAdapterError`` only for a problem with this
    writer's own understanding of the interface (an unrecognized status,
    a colliding output root, or an output root that already has a run) --
    never for a legitimate ``empty``/``insufficient``/``native_unavailable``/
    ``failed`` hierarchy-core result.
    """

    # Publishing-boundary guard (defect-1 fix): the writer itself -- not an
    # optional caller-side CLI helper -- requires and validates the real W2
    # outcome type before anything else happens. A structurally invalid
    # outcome (e.g. a terminal cluster whose structural_height fails W2's
    # own validator) is rejected here, unconditionally, before any status
    # branching, row building, or serialization is attempted.
    outcome = assert_native_w2_outcome(outcome)

    status = outcome.status
    if status not in _ACCEPTED_STATUSES:
        raise DerivedHierarchyAdapterError(
            "DERIVED_HIERARCHY_UNKNOWN_STATUS",
            f"hierarchy outcome reported an unrecognized status {status!r}; "
            f"expected one of {sorted(_ACCEPTED_STATUSES)}",
        )
    all_protected_roots: dict[str, Path] = dict(protected_roots or {})
    # Force-assigned last (bug fix): a caller-supplied protected_roots dict
    # must never be able to evict this mandatory protection by reusing its
    # key -- the sealed L4 run root is always protected, no matter what the
    # caller passes.
    all_protected_roots["sealed L4 run"] = sealed.root
    _check_output_root_disjoint(output_root, all_protected_roots)
    _check_output_root_not_already_published(output_root)
    entity_evidence = entity_evidence or {}
    relationship_evidence = relationship_evidence or {}
    _check_lineage_and_call_context(
        outcome,
        sealed,
        run_id=run_id,
        entity_evidence=entity_evidence,
        relationship_evidence=relationship_evidence,
    )

    run_context = {
        "run_id": run_id,
        "config_id": config_id,
        "policy_id": policy_id,
        "seed": seed,
        "hierarchy_version": outcome.hierarchy_version,
    }

    rows_by_table = _build_rows(outcome, run_context)
    table_payloads: dict[str, bytes] = {}
    row_counts: dict[str, int] = {}
    for name in _TABLE_ORDER:
        rows = rows_by_table[name]
        table_payloads[name] = _parquet_bytes(rows, DERIVED_HIERARCHY_TABLE_SCHEMAS[name])
        row_counts[name] = len(rows)

    manifest = _output_manifest(
        sealed,
        outcome=outcome,
        table_payloads=table_payloads,
        row_counts=row_counts,
        run_id=run_id,
        config_id=config_id,
        policy_id=policy_id,
        seed=seed,
        max_cluster_size=max_cluster_size,
        requested_provider_name=requested_provider_name,
        requested_provider_version=requested_provider_version,
    )
    manifest_payload = (canonical_json(manifest.model_dump(mode="json")) + "\n").encode(
        "utf-8"
    )

    receipt = _receipt(
        sealed,
        outcome=outcome,
        output_manifest=manifest,
        run_id=run_id,
        config_id=config_id,
        policy_id=policy_id,
        seed=seed,
        max_cluster_size=max_cluster_size,
        requested_provider_name=requested_provider_name,
        requested_provider_version=requested_provider_version,
        entity_evidence=entity_evidence,
        relationship_evidence=relationship_evidence,
    )
    receipt_payload = (canonical_json(receipt.model_dump(mode="json")) + "\n").encode(
        "utf-8"
    )

    # Every table/manifest/receipt validated successfully above -- now, and
    # only now, is anything written, and only into a private temp staging
    # directory that nothing else can observe.
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = output_root.parent / f".{output_root.name}.tmp-{uuid.uuid4().hex}"
    staging_dir.mkdir(parents=False, exist_ok=False)
    try:
        table_paths: dict[str, Path] = {}
        for name in _TABLE_ORDER:
            staged_path = staging_dir / _TABLE_FILE_NAMES[name]
            _write_bytes_durably(staged_path, table_payloads[name])
            table_paths[name] = output_root / _TABLE_FILE_NAMES[name]

        _write_bytes_durably(staging_dir / _MANIFEST_FILE_NAME, manifest_payload)
        _write_bytes_durably(staging_dir / _RECEIPT_FILE_NAME, receipt_payload)

        # Fsync the staging directory entry itself (not just its file
        # contents above) so the directory's own metadata -- the fact that
        # these files now exist in it -- is durable before the rename that
        # makes the run visible.
        _fsync_path(staging_dir)

        # Cheap advance check for a clear, fast error in the common case;
        # this alone is still racy (another writer can create output_root
        # between this check and the rename below), so the rename itself
        # must independently refuse to replace an existing destination --
        # see ``_exclusive_publish_rename``.
        _check_output_root_not_already_published(output_root)
        published = False
        try:
            _exclusive_publish_rename(staging_dir, output_root)
            published = True
            # Fsync the parent directory after rename so the rename itself
            # -- the directory entry now pointing at output_root instead of
            # the staging name -- is durable, not just the file contents
            # within it.
            _fsync_path(output_root.parent)
        except BaseException as exc:
            if not published:
                # The rename itself never happened (or failed outright):
                # output_root was never touched, and the staging directory
                # below is cleaned up by the outer except clause exactly as
                # before -- a genuine "nothing was published" failure.
                raise
            # The rename *succeeded* -- output_root now holds the fully
            # validated, complete run -- and only the best-effort
            # post-rename durability fsync of the parent directory entry
            # failed afterward. This is never "nothing was written": the
            # run is real and on disk. Surface a distinct, unambiguous
            # error code so callers can never mistake this for an
            # unpublished run (and must not retry into or delete
            # output_root as if it were empty); the normal staging-cleanup
            # path below is also skipped since staging_dir no longer exists
            # (it *is* output_root now).
            raise DerivedHierarchyAdapterError(
                "DERIVED_HIERARCHY_POST_PUBLISH_FSYNC_FAILED",
                f"run at {output_root} was published successfully (all "
                "tables, manifest, and receipt are durably on disk and "
                f"visible) but the best-effort fsync of its parent "
                f"directory entry afterward failed ({exc!r}); do not "
                "retry this output_root or delete it -- investigate "
                "filesystem durability directly",
            ) from exc
    except DerivedHierarchyAdapterError as exc:
        if exc.code == "DERIVED_HIERARCHY_POST_PUBLISH_FSYNC_FAILED":
            # output_root is the real, published run; staging_dir has
            # already been consumed by the rename -- do not touch either.
            raise
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    manifest_path = output_root / _MANIFEST_FILE_NAME
    receipt_path = output_root / _RECEIPT_FILE_NAME

    return DerivedHierarchyWriteResult(
        output_root=output_root,
        table_paths=table_paths,
        manifest_path=manifest_path,
        receipt_path=receipt_path,
        manifest=manifest,
        receipt=receipt,
    )
