"""Real-W2-core compatibility proof for the derived-hierarchy writer.

Scope note (W3, offline hierarchy integration)
-----------------------------------------------
``test_derived_hierarchy_artifact.py`` proves the writer against W3-owned
*synthetic* ``HierarchyOutcomeLike`` fixtures only -- those fixtures mirror
W2's real dataclasses by field name/shape (confirmed by direct source
read), but they are not W2's actual code and must never be cited as proof
of native W2 acceptance.

This module closes that gap using W2's **real, applied** hierarchy-core
modules now present natively in this worktree at
``fabric_kg_builder.graph.community_contracts`` /
``community_hierarchy_v2`` / ``community_provider_testing`` (applied from
the coordinator's checksum-verified
``w2-derived-hierarchy-v2-revised.patch``,
sha256 ``7bc91903d37b63e1a13e9a1b2e32a51a6b089afd2e02da5b624dd19e0b8b11e7``).
No test-process-only ``__path__`` overlay is needed any more -- the real
W2 source is a normal in-tree import.

Every test here constructs real ``DerivedHierarchyOutcome`` /
``DerivedClusterRow`` / ``DerivedMembershipRow`` instances by calling W2's
real ``build_derived_community_hierarchy_safe`` entrypoint with W2's own
test-only ``DeterministicConnectedComponentsProvider`` fake (never a
graspologic-native call -- that package is declared as an optional
``leiden`` extra by W2's patch but is not installed and stays out of
scope here), then round-trips the resulting *real* outcome object through
this worktree's ``write_derived_hierarchy_artifact``.

W2's revised core now computes ``execution_fingerprint`` as a bare, full
64-hex SHA-256 digest (``compute_execution_fingerprint`` ->
``canonical_sha256(payload)`` -- confirmed by direct source read of
``graph/community_identity.py``, which documents this as "a breaking
shape change from the previous ``'dfingerprint:' + 32 hex`` id-shaped
output"). The previously-known fingerprint32-vs-Sha256 mismatch is
therefore resolved on W2's side; this module asserts the *current* real
shape (bare 64-hex, i.e. genuinely ``Sha256``-shaped) rather than assuming
the old mismatch still holds. ``input_graph_hash`` was already a bare,
full 64-hex SHA-256 and remains so.

W3's own writer/receipt contract is NOT modified by this finding: the
receipt's ``execution_fingerprint``/``input_graph_hash`` fields stay typed
as ``RequiredText | None`` (not narrowed to ``Sha256``) -- loosening that
typing would be a W3-owned contract change outside today's authorized
scope, and nothing here depends on the field being narrowed.

W2's real core modules are a mandatory, accepted dependency for this
module: there is no try/except-Exception import guard and no
``pytest.skip`` fallback. If W2's real core modules are not importable in
this environment, every test in this module fails loudly at collection
time -- never silently skipped or treated as passing.

Every outcome round-tripped here is built from a real sealed-L4 fixture's
*own* entity/relationship rows (never a post-hoc ``sealed.entities``
replacement with ids the sealed run never actually sealed), so each
sealed-input/outcome pair proven here is genuinely self-consistent.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.graph import community_contracts as _real_contracts
from fabric_kg_builder.graph import community_hierarchy_v2 as _real_hierarchy_v2
from fabric_kg_builder.graph import community_provider_testing as _real_provider_testing
from tests.unit.test_derived_hierarchy_artifact import _sealed, _write
from tests.unit.test_graph_community import _make_entity, _make_relationship

# No try/except-Exception/pytest.skip import masking here: the real W2
# hierarchy-core modules are a mandatory, accepted dependency for this
# module's compatibility proof. If they are not importable, every test in
# this file fails loudly at collection time -- never silently skipped.


def _entities_and_relationships_from_sealed(
    sealed,
) -> tuple[list[EntityRow], list[RelationshipRow]]:
    """Derive W2-core inputs *from* a real sealed-L4 fixture's own rows.

    This feeds W2's real ``build_derived_community_hierarchy_safe`` with
    entity/relationship ids that are *already* members of ``sealed``'s own
    ``entities``/``relationships`` tuples -- a genuinely self-consistent
    sealed-input/outcome pair, never an ad hoc replacement of
    ``sealed.entities`` with ids the sealed run never actually sealed.
    """

    entities = [_make_entity(str(row["entity_id"])) for row in sealed.entities]
    relationships = [
        _make_relationship(
            str(row["source_entity_id"]), str(row["target_entity_id"])
        ).model_copy(
            update={
                "relationship_id": str(row["relationship_id"]),
                "relationship_type": str(row["semantic_relationship_id"]),
            }
        )
        for row in sealed.relationships
    ]
    return entities, relationships


def _assert_sha256_shaped(value: str) -> None:
    """Fail loudly (not silently) if *value* is not a bare 64-hex digest."""
    assert len(value) == 64, f"expected 64-hex SHA-256, got {len(value)} chars: {value!r}"
    assert all(c in "0123456789abcdef" for c in value), f"not lowercase-hex: {value!r}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_real_w2_complete_outcome_round_trips_through_writer(tmp_path: Path) -> None:
    # Positive integration fixture: a real sealed-L4 run (receipt/
    # manifest/projection/rows all genuinely produced by the sealed
    # pipeline), with the W2 outcome built *from that same sealed run's*
    # own entity/relationship rows -- never a post-hoc id swap.
    sealed = _sealed(tmp_path)
    entities, relationships = _entities_and_relationships_from_sealed(sealed)
    assert len(entities) == 2  # record + subject, from the sealed fixture
    assert len(relationships) == 1

    outcome = _real_hierarchy_v2.build_derived_community_hierarchy_safe(
        entities,
        relationships,
        provider=_real_provider_testing.DeterministicConnectedComponentsProvider(),
        policy_id="policy-1",
        run_id="run-real-w2-complete",
        max_cluster_size=50,
    )

    assert outcome.status == "complete"
    assert len(outcome.clusters) == 1
    assert len(outcome.memberships) == 2

    # W2's revised core now emits a bare, full 64-hex SHA-256 digest here
    # (no more "dfingerprint:" + 32-hex prefix shape) -- asserted against
    # the real value, not assumed.
    assert outcome.execution_fingerprint is not None
    _assert_sha256_shaped(outcome.execution_fingerprint)
    assert outcome.input_graph_hash is not None
    _assert_sha256_shaped(outcome.input_graph_hash)

    result = _write(
        tmp_path, sealed, outcome, suffix="real-w2-complete", run_id="run-real-w2-complete"
    )

    assert result.receipt.status == "complete"
    assert result.receipt.execution_fingerprint == outcome.execution_fingerprint
    assert result.receipt.input_graph_hash == outcome.input_graph_hash

    clusters_table = pq.read_table(result.table_paths["derived_community_clusters"])
    assert clusters_table.num_rows == 1
    memberships_table = pq.read_table(
        result.table_paths["derived_community_memberships"]
    )
    assert memberships_table.num_rows == 2


@pytest.mark.unit
def test_real_w2_empty_outcome_round_trips_through_writer(tmp_path: Path) -> None:
    # The outcome's own real-W2 entity universe is genuinely empty ([], []
    # into build_derived_community_hierarchy_safe), so the sealed input it
    # is paired with must also be genuinely zero-entity end-to-end (not a
    # nonempty sealed fixture paired post-hoc with an empty outcome) --
    # otherwise this would not be a legitimately self-consistent
    # sealed-input/outcome pair and the writer's entity-universe
    # conservation check correctly rejects it.
    sealed = _sealed(tmp_path, mutate=lambda candidates, work_unit: [])
    assert sealed.entities == ()

    outcome = _real_hierarchy_v2.build_derived_community_hierarchy_safe(
        [],
        [],
        provider=_real_provider_testing.DeterministicConnectedComponentsProvider(),
        policy_id="policy-1",
        run_id="run-real-w2-empty",
    )

    assert outcome.status == "empty"
    assert outcome.clusters == []
    assert outcome.memberships == []

    result = _write(
        tmp_path, sealed, outcome, suffix="real-w2-empty", run_id="run-real-w2-empty"
    )

    assert result.receipt.status == "empty"
    clusters_table = pq.read_table(result.table_paths["derived_community_clusters"])
    assert clusters_table.num_rows == 0


@pytest.mark.unit
def test_real_w2_insufficient_outcome_round_trips_through_writer(tmp_path: Path) -> None:
    # Same real sealed-L4 run's own entities, zero connecting (non-self-
    # loop) relationships -> every entity is its own singleton terminal:
    # real W2 INSUFFICIENT. Still a genuinely self-consistent pair -- the
    # entities really are this sealed run's entities, just without its
    # one relationship row.
    sealed = _sealed(tmp_path)
    entities, _relationships = _entities_and_relationships_from_sealed(sealed)
    assert len(entities) == 2

    outcome = _real_hierarchy_v2.build_derived_community_hierarchy_safe(
        entities,
        [],
        provider=_real_provider_testing.DeterministicConnectedComponentsProvider(),
        policy_id="policy-1",
        run_id="run-real-w2-insufficient",
    )

    assert outcome.status == "insufficient"
    assert len(outcome.clusters) == 2
    assert all(row.is_singleton_isolate for row in outcome.clusters)

    result = _write(
        tmp_path,
        sealed,
        outcome,
        suffix="real-w2-insufficient",
        run_id="run-real-w2-insufficient",
    )

    assert result.receipt.status == "insufficient"
    clusters_table = pq.read_table(result.table_paths["derived_community_clusters"])
    assert clusters_table.num_rows == 2


@pytest.mark.unit
def test_real_w2_native_unavailable_outcome_round_trips_through_writer(
    tmp_path: Path,
) -> None:
    sealed = _sealed(tmp_path)
    entities, relationships = _entities_and_relationships_from_sealed(sealed)

    class _AlwaysUnavailableProvider:
        provider_id = "stub-always-unavailable"
        provider_version = "0.0.0-test"

        def partition(self, edges, *, max_cluster_size, seed):
            del edges, max_cluster_size, seed
            raise _real_contracts.NativeProviderUnavailableError()

    outcome = _real_hierarchy_v2.build_derived_community_hierarchy_safe(
        entities,
        relationships,
        provider=_AlwaysUnavailableProvider(),
        policy_id="policy-1",
        run_id="run-real-w2-native-unavailable",
    )

    assert outcome.status == "native_unavailable"
    # Confirmed real-W2 invariant: no execution identity for this status.
    assert outcome.execution_fingerprint is None
    assert outcome.input_graph_hash is None

    result = _write(
        tmp_path,
        sealed,
        outcome,
        suffix="real-w2-native-unavailable",
        run_id="run-real-w2-native-unavailable",
    )

    assert result.receipt.status == "native_unavailable"
    assert result.receipt.execution_fingerprint is None
    assert result.receipt.input_graph_hash is None


@pytest.mark.unit
def test_real_w2_failed_outcome_round_trips_through_writer(tmp_path: Path) -> None:
    # Duplicate entity_id -> real W2 CanonicalProjectionError -> FAILED.
    # A failed outcome carries no clusters/memberships to cross-reference
    # against the sealed input, so this remains a plain, unmodified real
    # sealed-L4 fixture -- no entity-id alignment is needed or claimed.
    entities = [_make_entity("dup"), _make_entity("dup")]

    outcome = _real_hierarchy_v2.build_derived_community_hierarchy_safe(
        entities,
        [],
        provider=_real_provider_testing.DeterministicConnectedComponentsProvider(),
        policy_id="policy-1",
        run_id="run-real-w2-failed",
    )

    assert outcome.status == "failed"
    assert outcome.reason  # the real validation failure, verbatim
    assert outcome.execution_fingerprint is None

    sealed = _sealed(tmp_path)
    result = _write(
        tmp_path, sealed, outcome, suffix="real-w2-failed", run_id="run-real-w2-failed"
    )

    assert result.receipt.status == "failed"
    assert result.receipt.reason == outcome.reason
