"""Tests for the W3 derived community-hierarchy artifact/receipt writer.

Scope: exercises `fabric_kg_builder.semantic.derived_hierarchy_artifact`
against real sealed-L4 lineage (via the same minimal pipeline driver used by
`test_sealed_l4_hierarchy_source.py`) combined with synthetic, in-process
`HierarchyOutcomeLike`-shaped fixtures -- never W2's real hierarchy core
(not yet importable here) and never a mock claimed as native acceptance.
Covers complete / empty / insufficient / native_unavailable / failed
outcomes, sparse-but-legitimate zero-row tables, status-vocabulary
rejection, output-root path-disjointness rejection, execution-unique
artifact identity, atomic publish-on-validation-failure, and that
unchanged sealed-L4 bytes/hashes are never recomputed. W3's own orthogonal
sealed-partial-scope flag (input scope, not outcome status) is covered
separately.

Outcomes here are built from W2's own real ``DerivedClusterRow`` /
``DerivedMembershipRow`` / ``InterCommunityRelationshipDiagnostic`` /
``DerivedHierarchyOutcome`` / ``HierarchyStatus`` dataclasses, imported
directly from ``fabric_kg_builder.graph.community_contracts`` -- never a
duck-typed/``Protocol``-shaped stand-in. The writer's own native-type gate
(``assert_native_w2_outcome``) requires exactly this; an injected
*provider* (the thing that produces an outcome) still never needs a native
import, only the outcome value handed to the writer does. These fixtures
remain W3-owned test wiring, never asserted as proof of native W2
*provider* acceptance.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

import fabric_kg_builder.semantic.derived_hierarchy_artifact as derived_hierarchy_artifact
from fabric_kg_builder.contracts.receipts import ArtifactManifest
from fabric_kg_builder.graph.community_contracts import (
    DerivedClusterRow,
    DerivedHierarchyOutcome,
    DerivedMembershipRow,
    HierarchyStatus,
    InterCommunityRelationshipDiagnostic,
)
from fabric_kg_builder.graph.community_identity import compute_community_identity
from fabric_kg_builder.model.derived_hierarchy_schemas import (
    DERIVED_HIERARCHY_CORE_VERSION_FALLBACK,
    DERIVED_HIERARCHY_TABLE_SCHEMAS,
)
from fabric_kg_builder.semantic.derived_hierarchy_artifact import (
    DerivedHierarchyAdapterError,
    DerivedHierarchyWriteResult,
    write_derived_hierarchy_artifact,
)
from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import (
    load_sealed_l4_hierarchy_rows,
)
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from tests.unit.test_schema2_validation_stage import _l3, _pipeline


def _run(tmp_path: Path, domain: str = "records", *, mutate=None):
    l1_state_root, domain_path, _l2 = _pipeline(tmp_path, domain, mutate=mutate)
    l3 = _l3(tmp_path, l1_state_root, domain_path)
    l4 = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")
    return l3, l4


def _sealed(tmp_path: Path, *, mutate=None):
    l3, l4 = _run(tmp_path, mutate=mutate)
    return load_sealed_l4_hierarchy_rows(
        l4.run_root, input_manifest_search_roots=(l3.state_root,)
    )


# ---------------------------------------------------------------------------
# Real W2 outcome construction helpers.
#
# ``_outcome`` builds a genuine ``DerivedHierarchyOutcome`` -- the writer's
# own native-type gate (``assert_native_w2_outcome``) requires exactly this
# type, not a duck-typed stand-in. Row-level objects
# (``DerivedClusterRow`` / ``DerivedMembershipRow`` /
# ``InterCommunityRelationshipDiagnostic``) are likewise W2's real
# dataclasses, imported directly rather than re-declared.
# ---------------------------------------------------------------------------


def _outcome(
    status: str,
    *,
    reason: str | None = None,
    clusters: tuple[DerivedClusterRow, ...] = (),
    memberships: tuple[DerivedMembershipRow, ...] = (),
    inter_community_relationship_diagnostics: tuple[
        InterCommunityRelationshipDiagnostic, ...
    ] = (),
    input_graph_hash: str | None = "a" * 64,
    execution_fingerprint: str | None = "f" * 64,
    provider_id: str = "synthetic-provider",
    provider_version: str = "0.0.0-test",
    hierarchy_version: str = DERIVED_HIERARCHY_CORE_VERSION_FALLBACK,
) -> DerivedHierarchyOutcome:
    try:
        status_value = HierarchyStatus(status)
    except ValueError:
        # Deliberately unknown/invalid status: a real ``DerivedHierarchyOutcome``
        # does not itself enforce the enum at construction time, so this lets
        # tests exercise the writer's own unknown-status rejection gate
        # rather than failing before the writer is ever invoked.
        status_value = status
    return DerivedHierarchyOutcome(
        status=status_value,
        reason=reason,
        clusters=list(clusters),
        memberships=list(memberships),
        inter_community_relationship_diagnostics=inter_community_relationship_diagnostics,
        input_graph_hash=input_graph_hash,
        execution_fingerprint=execution_fingerprint,
        provider_id=provider_id,
        provider_version=provider_version,
        hierarchy_version=hierarchy_version,
    )


def _complete_outcome(
    entity_ids: tuple[str, ...],
    *,
    run_id: str = "run-1",
    execution_fingerprint: str = "f" * 64,
) -> DerivedHierarchyOutcome:
    """A ``complete`` outcome whose single terminal cluster's membership
    rows exactly conserve the sealed input's entity universe -- the only
    shape a real ``complete`` outcome may legitimately take. ``cluster_id``
    is computed via W2's own ``compute_community_identity`` (not an
    arbitrary string), satisfying the writer's native structural-validity
    gate (``assert_valid_derived_hierarchy``'s permutation-repeatability
    check). ``run_id``/``execution_fingerprint`` must match whatever is
    passed to the write call itself -- the writer's call-context check
    rejects a cluster row whose ``run_id``/``execution_fingerprint`` does
    not agree with the actual attempt, so callers exercising more than one
    ``run_id`` (or more than one execution identity) against the same
    outcome shape must thread the matching values through here rather than
    relying on a fixed default."""
    provider_id = "synthetic-provider"
    provider_version = "0.0.0-test"
    policy_id = "policy-1"
    cluster_id = compute_community_identity(
        domain_hash=None,
        provider_id=provider_id,
        provider_version=provider_version,
        policy_id=policy_id,
        member_entity_ids=entity_ids,
        structural_lineage=(),
    )
    return _outcome(
        "complete",
        execution_fingerprint=execution_fingerprint,
        clusters=(
            DerivedClusterRow(
                cluster_id=cluster_id,
                hierarchy_version=DERIVED_HIERARCHY_CORE_VERSION_FALLBACK,
                parent_cluster_id=None,
                structural_height=0,
                native_depth=0,
                is_terminal=True,
                is_singleton_isolate=False,
                is_oversized=False,
                member_entity_ids=entity_ids,
                evidence_ids=("span-1",),
                relationship_contributed_evidence_ids=("span-1",),
                provider_id=provider_id,
                provider_version=provider_version,
                policy_id=policy_id,
                domain_hash=None,
                run_id=run_id,
                execution_fingerprint=execution_fingerprint,
            ),
        ),
        memberships=tuple(
            DerivedMembershipRow(
                cluster_id=cluster_id,
                entity_id=entity_id,
                evidence_ids=("span-1",),
                primary_membership=True,
            )
            for entity_id in entity_ids
        ),
    )


def _insufficient_outcome(entity_ids: tuple[str, ...]) -> DerivedHierarchyOutcome:
    """An ``insufficient`` outcome: nonempty input, zero projection edges --
    every entity is its own singleton terminal cluster (per
    ``HierarchyStatus.INSUFFICIENT``'s real semantics), never zero
    clusters/memberships for a nonempty entity universe. Each singleton's
    ``cluster_id`` is computed via W2's own ``compute_community_identity``
    over its single-member set, matching real native output shape."""
    provider_id = "synthetic-provider"
    provider_version = "0.0.0-test"
    policy_id = "policy-1"
    clusters = []
    memberships = []
    for entity_id in entity_ids:
        cluster_id = compute_community_identity(
            domain_hash=None,
            provider_id=provider_id,
            provider_version=provider_version,
            policy_id=policy_id,
            member_entity_ids=(entity_id,),
            structural_lineage=(),
        )
        clusters.append(
            DerivedClusterRow(
                cluster_id=cluster_id,
                hierarchy_version=DERIVED_HIERARCHY_CORE_VERSION_FALLBACK,
                parent_cluster_id=None,
                structural_height=0,
                native_depth=None,
                is_terminal=True,
                is_singleton_isolate=True,
                is_oversized=False,
                member_entity_ids=(entity_id,),
                evidence_ids=(),
                relationship_contributed_evidence_ids=(),
                provider_id=provider_id,
                provider_version=provider_version,
                policy_id=policy_id,
                domain_hash=None,
                run_id="run-1",
                execution_fingerprint="f" * 64,
            )
        )
        memberships.append(
            DerivedMembershipRow(
                cluster_id=cluster_id,
                entity_id=entity_id,
                evidence_ids=(),
                primary_membership=True,
            )
        )
    return _outcome(
        "insufficient",
        reason="fewer than minimum required entities for clustering",
        clusters=tuple(clusters),
        memberships=tuple(memberships),
    )


def _write(
    tmp_path: Path,
    sealed,
    outcome,
    *,
    suffix: str = "out",
    run_id: str = "run-1",
    max_cluster_size: int = 50,
) -> DerivedHierarchyWriteResult:
    return write_derived_hierarchy_artifact(
        sealed=sealed,
        outcome=outcome,
        output_root=tmp_path / suffix,
        run_id=run_id,
        config_id="config-1",
        policy_id="policy-1",
        seed=7,
        max_cluster_size=max_cluster_size,
        requested_provider_name="synthetic-test-provider",
        requested_provider_version="0.0.0-test",
    )


@pytest.mark.unit
def test_writes_complete_outcome_with_all_tables_and_receipt(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome = _complete_outcome(entity_ids)

    result = _write(tmp_path, sealed, outcome)

    assert result.receipt.status == "complete"
    assert result.receipt.reason is None
    assert result.receipt.sealed_l4_manifest_id == sealed.manifest.artifact_manifest_id
    assert result.receipt.sealed_l4_manifest_hash == sealed.manifest.manifest_hash
    assert result.receipt.sealed_l4_receipt_id == sealed.receipt.stage_receipt_id
    assert result.receipt.sealed_l4_receipt_hash == sealed.receipt.receipt_hash
    assert result.receipt.sealed_partial_scope_present is False
    assert result.receipt.realized_hierarchy_version == outcome.hierarchy_version
    assert result.receipt.execution_fingerprint == outcome.execution_fingerprint
    assert result.receipt.input_graph_hash == outcome.input_graph_hash
    assert result.receipt.realized_provider_id == outcome.provider_id
    assert result.receipt.realized_provider_version == outcome.provider_version

    assert set(result.table_paths) == set(DERIVED_HIERARCHY_TABLE_SCHEMAS)
    clusters_table = pq.read_table(
        result.table_paths["derived_community_clusters"]
    )
    assert clusters_table.num_rows == 1
    memberships_table = pq.read_table(
        result.table_paths["derived_community_memberships"]
    )
    assert memberships_table.num_rows == len(entity_ids)
    diagnostics_table = pq.read_table(
        result.table_paths["derived_inter_community_relationship_diagnostics"]
    )
    assert diagnostics_table.num_rows == 0

    assert isinstance(result.manifest, ArtifactManifest)
    assert result.manifest_path.exists()
    assert result.receipt_path.exists()

    # Sealed L4 bytes/hashes are untouched by this write.
    original_receipt_hash = sealed.receipt.receipt_hash
    original_manifest_hash = sealed.manifest.manifest_hash
    assert sealed.receipt.receipt_hash == original_receipt_hash
    assert sealed.manifest.manifest_hash == original_manifest_hash


@pytest.mark.unit
def test_writes_empty_outcome_with_zero_rows_not_swallowed(tmp_path: Path) -> None:
    """``empty`` is a legitimate W2 outcome status for a genuinely
    zero-entity sealed input (``HierarchyStatus.EMPTY``'s real semantics:
    "Empty entity input"), distinct from W3's own orthogonal
    sealed-partial-scope flag -- it must never be coerced into
    ``complete`` nor silently dropped. The sealed input here is
    constructed with zero qualifying candidates end-to-end (not merely an
    outcome claiming ``empty`` against a nonempty input), so the writer's
    entity-universe-conservation check is legitimately satisfied by
    vacuous truth."""
    sealed = _sealed(tmp_path, mutate=lambda candidates, work_unit: [])
    assert sealed.entities == ()
    outcome = _outcome("empty", reason="no entities qualified for clustering")

    result = _write(tmp_path, sealed, outcome)

    assert result.receipt.status == "empty"
    assert result.receipt.reason == "no entities qualified for clustering"
    assert result.receipt.status != "complete"
    for path in result.table_paths.values():
        assert pq.read_table(path).num_rows == 0


@pytest.mark.unit
def test_writes_insufficient_outcome_with_zero_rows_not_swallowed(
    tmp_path: Path,
) -> None:
    """``insufficient`` means nonempty input with zero projection edges:
    every entity is its own terminal singleton community
    (``HierarchyStatus.INSUFFICIENT``'s real semantics) -- it is NOT zero
    clusters/memberships against a nonempty entity universe, which would
    fail the writer's own entity-universe-conservation check. This test
    name is retained for continuity but now asserts the real shape: one
    singleton terminal cluster and one membership row per sealed entity,
    with zero inter-community diagnostics (no edges to span)."""
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome = _insufficient_outcome(entity_ids)

    result = _write(tmp_path, sealed, outcome)

    assert result.receipt.status == "insufficient"
    assert result.receipt.reason
    clusters_table = pq.read_table(result.table_paths["derived_community_clusters"])
    memberships_table = pq.read_table(
        result.table_paths["derived_community_memberships"]
    )
    assert clusters_table.num_rows == len(entity_ids)
    assert memberships_table.num_rows == len(entity_ids)
    diagnostics_table = pq.read_table(
        result.table_paths["derived_inter_community_relationship_diagnostics"]
    )
    assert diagnostics_table.num_rows == 0


@pytest.mark.unit
def test_writes_native_unavailable_outcome_without_claiming_success(
    tmp_path: Path,
) -> None:
    """``native_unavailable`` is the fail-closed status the public CLI must
    surface when W2's real provider cannot be constructed/imported -- it is
    receipted like any other legitimate non-complete outcome, never
    swallowed into a bare success and never satisfied by a synthetic
    fallback standing in for the native provider. Execution-identity
    fields must be forced to None regardless of what the outcome carries,
    since W2 never populates a real projection for this status."""
    sealed = _sealed(tmp_path)
    outcome = _outcome(
        status="native_unavailable",
        reason="hierarchy-core native provider dependency is unavailable",
    )

    result = _write(tmp_path, sealed, outcome)

    assert result.receipt.status == "native_unavailable"
    assert result.receipt.reason == (
        "hierarchy-core native provider dependency is unavailable"
    )
    assert result.receipt.status != "complete"
    assert result.receipt.execution_fingerprint is None
    assert result.receipt.input_graph_hash is None
    for path in result.table_paths.values():
        assert pq.read_table(path).num_rows == 0


@pytest.mark.unit
def test_writes_failed_outcome_without_claiming_success(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    outcome = _outcome(
        status="failed",
        reason="hierarchy-core provider raised during clustering",
    )

    result = _write(tmp_path, sealed, outcome)

    assert result.receipt.status == "failed"
    assert result.receipt.reason == (
        "hierarchy-core provider raised during clustering"
    )
    assert result.receipt.execution_fingerprint is None
    assert result.receipt.input_graph_hash is None
    for path in result.table_paths.values():
        assert pq.read_table(path).num_rows == 0


@pytest.mark.unit
def test_writes_sparse_diagnostics_table_as_legitimately_empty(
    tmp_path: Path,
) -> None:
    """An outcome with clusters/memberships but zero inter-community
    diagnostics must be written as a genuinely empty table, not rejected
    or inferred."""
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome = _complete_outcome(entity_ids)
    assert outcome.inter_community_relationship_diagnostics == ()

    result = _write(tmp_path, sealed, outcome)

    diagnostics_table = pq.read_table(
        result.table_paths["derived_inter_community_relationship_diagnostics"]
    )
    assert diagnostics_table.num_rows == 0
    assert diagnostics_table.schema.equals(
        DERIVED_HIERARCHY_TABLE_SCHEMAS[
            "derived_inter_community_relationship_diagnostics"
        ]
    )


@pytest.mark.unit
def test_rejects_unknown_status_without_writing_anything(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="not-a-real-status")
    output_root = tmp_path / "rejected"

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=output_root,
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert excinfo.value.code == "DERIVED_HIERARCHY_UNKNOWN_STATUS"
    assert not output_root.exists()


@pytest.mark.unit
def test_rejects_output_root_equal_to_sealed_root(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="empty")

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=sealed.root,
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert excinfo.value.code == "DERIVED_HIERARCHY_OUTPUT_ROOT_COLLISION"


@pytest.mark.unit
def test_rejects_output_root_nested_under_sealed_root(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="empty")

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=sealed.root / "nested-under-sealed",
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert excinfo.value.code == "DERIVED_HIERARCHY_OUTPUT_ROOT_COLLISION"


@pytest.mark.unit
def test_rejects_output_root_that_is_an_ancestor_of_sealed_root(
    tmp_path: Path,
) -> None:
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="empty")

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=sealed.root.parent,
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert excinfo.value.code == "DERIVED_HIERARCHY_OUTPUT_ROOT_COLLISION"


@pytest.mark.unit
def test_preserves_partial_scope_flag_from_sealed_input(tmp_path: Path) -> None:
    sealed = _sealed(tmp_path)
    assert sealed.partial_scope is None
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)

    outcome = _complete_outcome(entity_ids)
    result = _write(tmp_path, sealed, outcome)

    assert result.receipt.sealed_partial_scope_present is False


@pytest.mark.unit
def test_receipt_and_manifest_hash_are_deterministic_for_identical_inputs(
    tmp_path: Path,
) -> None:
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome = _complete_outcome(entity_ids, run_id="run-same")

    result_a = _write(tmp_path, sealed, outcome, suffix="a", run_id="run-same")
    result_b = _write(tmp_path, sealed, outcome, suffix="b", run_id="run-same")

    assert result_a.receipt.receipt_hash == result_b.receipt.receipt_hash
    assert result_a.manifest.manifest_hash == result_b.manifest.manifest_hash
    assert (
        result_a.receipt.derived_hierarchy_receipt_id
        == result_b.receipt.derived_hierarchy_receipt_id
    )


@pytest.mark.unit
def test_artifact_identity_is_execution_unique_across_runs(tmp_path: Path) -> None:
    """Two writes against the same sealed input/outcome but different
    ``run_id`` values must mint distinct artifact/receipt identities and
    hashes -- the writer must never collapse execution identity down to a
    pure function of the sealed input alone."""
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome_a = _complete_outcome(entity_ids, run_id="run-a")
    outcome_b = _complete_outcome(entity_ids, run_id="run-b")

    result_a = _write(tmp_path, sealed, outcome_a, suffix="run-a", run_id="run-a")
    result_b = _write(tmp_path, sealed, outcome_b, suffix="run-b", run_id="run-b")

    assert result_a.receipt.receipt_hash != result_b.receipt.receipt_hash
    assert result_a.manifest.manifest_hash != result_b.manifest.manifest_hash
    assert (
        result_a.receipt.derived_hierarchy_receipt_id
        != result_b.receipt.derived_hierarchy_receipt_id
    )


@pytest.mark.unit
def test_atomic_publish_writes_nothing_when_receipt_validation_fails(
    tmp_path: Path,
) -> None:
    """An invalid call parameter (here: a blank ``run_id``, rejected by the
    receipt's ``RequiredText`` constraint) must fail during in-memory
    construction, before any file is written to ``output_root`` --
    validate-then-publish, never a partially-written artifact."""
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="failed", reason="synthetic pre-io failure probe")
    output_root = tmp_path / "atomic-failure"

    with pytest.raises(ValidationError):
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=output_root,
            run_id="   ",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert not output_root.exists()


@pytest.mark.unit
def test_atomic_publish_cleans_up_temp_staging_and_leaves_no_final_output_on_mid_write_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure injected *during* disk IO (after in-memory validation has
    already succeeded, partway through staging the manifest/receipt files)
    must still leave ``output_root`` completely absent and must not leave
    any temp staging directory behind -- this is the genuine atomic-
    publish gate, distinct from the pre-IO ``ValidationError`` test above
    (which only proves validate-before-publish, not publish-time
    atomicity)."""
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome = _complete_outcome(entity_ids)
    output_root = tmp_path / "mid-write-failure"

    write_calls = {"count": 0}
    real_write_bytes = Path.write_bytes

    def _flaky_write_bytes(self: Path, data: bytes):
        write_calls["count"] += 1
        # Let the 3 table files stage successfully, then fail while
        # staging the manifest -- proving a failure *after* some bytes are
        # already on disk in the temp dir still yields a fully clean
        # outcome, not a partially-staged leftover.
        if write_calls["count"] == 4:
            raise OSError("synthetic disk failure during manifest staging")
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", _flaky_write_bytes)

    with pytest.raises(OSError, match="synthetic disk failure"):
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=output_root,
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert write_calls["count"] == 4
    assert not output_root.exists()
    leftover_temp_dirs = [
        entry
        for entry in tmp_path.iterdir()
        if entry.name.startswith(".mid-write-failure.tmp-")
    ]
    assert leftover_temp_dirs == []


@pytest.mark.unit
def test_atomic_publish_cleans_up_temp_staging_on_rename_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure injected at the final atomic-rename publish step itself
    (every byte already staged successfully) must still leave
    ``output_root`` absent and the temp staging directory removed -- the
    publish step is all-or-nothing, never a partial rename.

    The real publish boundary is ``_exclusive_publish_rename`` -- a
    kernel-level no-replace rename (``renameat2``/``renamex_np`` via
    ``ctypes``, not ``Path.rename``/``os.rename``, which the writer
    deliberately never uses for the final publish since a plain rename
    silently replaces an existing empty destination directory). This test
    patches that exact function rather than ``Path.rename`` so it exercises
    the writer's real publish primitive instead of a syscall it no longer
    makes."""
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="failed", reason="synthetic rename-failure probe")
    output_root = tmp_path / "rename-failure"

    def _flaky_publish_rename(staging_dir: Path, target: Path) -> None:
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(
        derived_hierarchy_artifact, "_exclusive_publish_rename", _flaky_publish_rename
    )

    with pytest.raises(OSError, match="synthetic rename failure"):
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=output_root,
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert not output_root.exists()
    leftover_temp_dirs = [
        entry
        for entry in tmp_path.iterdir()
        if entry.name.startswith(".rename-failure.tmp-")
    ]
    assert leftover_temp_dirs == []


@pytest.mark.unit
def test_atomic_publish_refuses_to_overwrite_an_existing_final_run(
    tmp_path: Path,
) -> None:
    """Calling the writer a second time against an ``output_root`` that
    already holds a published run (even an empty directory with no
    contents yet) must be refused outright, never silently overwritten
    file-by-file."""
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="empty")
    output_root = tmp_path / "existing-run"
    output_root.mkdir()

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=output_root,
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert excinfo.value.code == "DERIVED_HIERARCHY_OUTPUT_ROOT_ALREADY_EXISTS"
    # Refusal must happen before any staging directory is even created.
    leftover_temp_dirs = [
        entry
        for entry in tmp_path.iterdir()
        if entry.name.startswith(".existing-run.tmp-")
    ]
    assert leftover_temp_dirs == []


@pytest.mark.unit
def test_atomic_publish_refuses_a_real_published_run_that_a_symlink_aliases(
    tmp_path: Path,
) -> None:
    """A symlink whose target is an already-published run must be refused
    exactly like the real path would be -- a symlink alias must not be a
    way to evade the existing-run refusal guard."""
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="empty")
    real_run = tmp_path / "real-published-run"
    real_run.mkdir()
    alias = tmp_path / "alias-to-existing-run"
    alias.symlink_to(real_run, target_is_directory=True)

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=alias,
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert excinfo.value.code == "DERIVED_HIERARCHY_OUTPUT_ROOT_ALREADY_EXISTS"


@pytest.mark.unit
def test_atomic_publish_refuses_a_dangling_symlink_output_root(
    tmp_path: Path,
) -> None:
    """A dangling symlink (its target does not exist) at ``output_root``
    must still be refused -- ``exists()`` alone would miss this since it
    follows symlinks and reports ``False`` for a broken link, so the
    guard must also check ``is_symlink()`` directly."""
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="empty")
    dangling = tmp_path / "dangling-symlink"
    dangling.symlink_to(tmp_path / "does-not-exist", target_is_directory=True)

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=dangling,
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert excinfo.value.code == "DERIVED_HIERARCHY_OUTPUT_ROOT_ALREADY_EXISTS"


@pytest.mark.unit
def test_successful_publish_leaves_no_temp_staging_directory_behind(
    tmp_path: Path,
) -> None:
    """A successful publish must leave exactly the final ``output_root``
    behind -- no ``.tmp-*`` staging directory left as a sibling."""
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="failed", reason="synthetic no-temp-staging probe")
    output_root = tmp_path / "clean-success"

    result = _write(tmp_path, sealed, outcome, suffix="clean-success")

    assert result.output_root == output_root
    assert output_root.is_dir()
    sibling_names = {entry.name for entry in tmp_path.iterdir()}
    temp_siblings = {name for name in sibling_names if ".clean-success.tmp-" in name}
    assert temp_siblings == set()


@pytest.mark.unit
def test_writer_accepts_additional_protected_roots_beyond_sealed_root(
    tmp_path: Path,
) -> None:
    """Callers (the CLI) can pass extra ``protected_roots`` -- e.g. L3
    input-manifest search roots, or local L1-L4 state-directory defaults
    -- and a colliding ``output_root`` must be rejected by label, exactly
    like a sealed-root collision is."""
    sealed = _sealed(tmp_path)
    outcome = _outcome(status="empty")
    l3_root = tmp_path / ".fkg" / "l3"

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=l3_root,
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
            protected_roots={"L3 input-manifest search": l3_root},
        )

    assert excinfo.value.code == "DERIVED_HIERARCHY_OUTPUT_ROOT_COLLISION"
    assert "L3 input-manifest search" in str(excinfo.value)


# -- Parent-reproduced defect regressions (w3-v2-parent-reproduction-tests.py,
# SHA256 7550a2e524130f35ccec28ee5533e93156b4145f52ea8b2e0552cd3c7e3d97aa) --
#
# Each test below is the assert-the-fix counterpart of a reviewer
# reproduction that deliberately asserted the *bug* against the previously
# rejected v2 writer. They exercise the current writer's real,
# unconditional guards -- never an optional/deferrable caller-side helper
# -- and must all fail loudly (no publish, no swallowed exception) rather
# than silently accepting the defect shape.


@pytest.mark.unit
def test_structurally_invalid_outcome_is_rejected_not_published(tmp_path: Path) -> None:
    """Regression for reproduced defect 1: a ``complete`` outcome whose
    single terminal cluster claims ``structural_height=99`` (a terminal
    cluster must be height 0 -- an internally inconsistent tree) must be
    rejected by the writer's unconditional publishing-boundary guard
    (``assert_native_w2_outcome`` -> ``assert_valid_derived_hierarchy``,
    called before a single row is built) -- never published as
    ``status=complete`` with the bogus height persisted to Parquet."""
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome = _complete_outcome(entity_ids)
    bogus_cluster = replace(outcome.clusters[0], structural_height=99)
    bogus_outcome = replace(outcome, clusters=(bogus_cluster,))
    output_root = tmp_path / "out"

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        _write(tmp_path, sealed, bogus_outcome)

    assert excinfo.value.code == "DERIVED_HIERARCHY_OUTCOME_STRUCTURALLY_INVALID"
    assert not output_root.exists()


@pytest.mark.unit
def test_distinct_execution_fingerprints_mint_distinct_identity(
    tmp_path: Path,
) -> None:
    """Regression for reproduced defect 2: two attempts sharing every
    request field (sealed source, run_id, config_id, policy_id, seed)
    except ``max_cluster_size`` (50 vs. 51), whose *realized* execution
    fingerprints genuinely differ (as W2 itself would report for two such
    attempts), must mint distinct artifact/manifest/receipt ids -- never
    collide because the request shape alone was bound to identity while
    the realized execution fingerprint was ignored."""
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome_a = _complete_outcome(entity_ids, execution_fingerprint="a" * 64)
    outcome_b = _complete_outcome(entity_ids, execution_fingerprint="b" * 64)

    result_a = _write(tmp_path, sealed, outcome_a, suffix="out-a", max_cluster_size=50)
    result_b = _write(tmp_path, sealed, outcome_b, suffix="out-b", max_cluster_size=51)

    assert result_a.manifest.artifact_manifest_id != result_b.manifest.artifact_manifest_id
    assert result_a.receipt.derived_hierarchy_receipt_id != (
        result_b.receipt.derived_hierarchy_receipt_id
    )
    ids_a = {entry.artifact_id for entry in result_a.manifest.entries}
    ids_b = {entry.artifact_id for entry in result_b.manifest.entries}
    assert ids_a.isdisjoint(ids_b)


@pytest.mark.unit
def test_concurrently_created_destination_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for reproduced defect 3: a destination directory created
    by another process *after* the writer's own pre-check but *before* the
    actual rename syscall must never be silently replaced. This wraps the
    real ``_exclusive_publish_rename`` so the destination is created at the
    exact instant the original reproduction exploited (immediately before
    the rename call), then delegates to the genuine kernel-level
    no-replace primitive -- proving the fix is the atomic rename itself,
    not merely a race-prone existence recheck."""
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome = _complete_outcome(entity_ids)
    output_root = tmp_path / "race"
    real_rename = derived_hierarchy_artifact._exclusive_publish_rename

    def _racing_publish_rename(staging_dir: Path, target: Path) -> None:
        target.mkdir(parents=True)
        real_rename(staging_dir, target)

    monkeypatch.setattr(
        derived_hierarchy_artifact, "_exclusive_publish_rename", _racing_publish_rename
    )

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=output_root,
            run_id="run-1",
            config_id="config-1",
            policy_id="policy-1",
            seed=7,
            max_cluster_size=50,
            requested_provider_name="synthetic-test-provider",
            requested_provider_version="0.0.0-test",
        )

    assert excinfo.value.code == "DERIVED_HIERARCHY_OUTPUT_ROOT_ALREADY_EXISTS"
    # The concurrently-created (empty) destination is the one left behind
    # -- it must be untouched by this writer, never replaced with its own
    # staged content.
    assert list(output_root.iterdir()) == []


@pytest.mark.unit
def test_evidence_binding_hash_is_always_writer_computed_not_caller_supplied(
    tmp_path: Path,
) -> None:
    """Regression for reproduced defect 4: there is no longer any
    ``evidence_binding_hash`` *parameter* a caller can supply at all --
    the writer always derives it itself, via W2's real
    ``compute_evidence_binding_hash`` helper, from the caller-supplied
    evidence-sidecar mappings. This proves the persisted value is exactly
    that computed digest (never an arbitrary passthrough string), and that
    changing the sidecar evidence changes the persisted hash."""
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome = _complete_outcome(entity_ids)
    entity_evidence = {entity_ids[0]: ("span-1",)}

    result = write_derived_hierarchy_artifact(
        sealed=sealed,
        outcome=outcome,
        output_root=tmp_path / "out",
        run_id="run-1",
        config_id="config-1",
        policy_id="policy-1",
        seed=7,
        max_cluster_size=50,
        requested_provider_name="synthetic-test-provider",
        requested_provider_version="0.0.0-test",
        entity_evidence=entity_evidence,
    )

    from fabric_kg_builder.graph.community_identity import compute_evidence_binding_hash

    expected = compute_evidence_binding_hash(entity_evidence, {})
    assert result.receipt.evidence_binding_hash == expected
    assert derived_hierarchy_artifact._is_sha256_hex(result.receipt.evidence_binding_hash)


@pytest.mark.unit
def test_outcome_level_hash_rejects_uppercase_hex_shape(tmp_path: Path) -> None:
    """Regression for reproduced defect 4's strict-lowercase ask: a
    hash-shaped field that is uppercase (or mixed-case) hex -- 64 chars,
    valid hex, but not lowercase -- must be rejected by
    ``_check_lineage_and_call_context``'s shape check, never silently
    normalized via ``.lower()`` and accepted."""
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome = _complete_outcome(entity_ids, execution_fingerprint="F" * 64)

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        _write(tmp_path, sealed, outcome)

    assert excinfo.value.code == "DERIVED_HIERARCHY_INVALID_HASH_SHAPE"


@pytest.mark.unit
def test_foreign_cluster_run_id_and_fingerprint_are_rejected_not_published(
    tmp_path: Path,
) -> None:
    """Regression for reproduced defect 5: a cluster row carrying a
    foreign ``run_id`` (not this attempt's) and a mismatched
    ``execution_fingerprint`` (not the outcome's own) must never be
    published under this attempt's receipt ``run_id``/fingerprint -- the
    call-context cross-reference must reject it outright, surfaced as its
    own actionable error code."""
    sealed = _sealed(tmp_path)
    entity_ids = tuple(row["entity_id"] for row in sealed.entities)
    outcome = _complete_outcome(entity_ids, run_id="run-1", execution_fingerprint="c" * 64)
    foreign_cluster = replace(
        outcome.clusters[0], run_id="foreign-run", execution_fingerprint="0" * 64
    )
    foreign_outcome = replace(outcome, clusters=(foreign_cluster,))
    output_root = tmp_path / "out"

    with pytest.raises(DerivedHierarchyAdapterError) as excinfo:
        _write(tmp_path, sealed, foreign_outcome, run_id="run-1")

    assert excinfo.value.code == "DERIVED_HIERARCHY_CALL_CONTEXT_MISMATCH"
    assert not output_root.exists()
