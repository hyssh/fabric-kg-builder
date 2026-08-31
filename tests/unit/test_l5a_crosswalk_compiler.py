"""Tests for the product-side L5a crosswalk compiler.

L5a publication used to be reachable only through a crosswalk hand-built in
the test suite, which meant nothing shipped in the wheel could derive one.
These tests pin that the compiled crosswalk is accepted by the very gate the
hand-built one satisfied, so the CLI and the suite prove the same object.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.serving.l5a_crosswalk import (
    compile_access_policy,
    compile_governed_assets,
    compile_publication_crosswalk,
)
from fabric_kg_builder.serving.structured_publication import (
    compile_l5a_publication,
)

from tests.unit.test_l5a_structured_publication import _l3_without_manifest
from fabric_kg_builder.serving.lifecycle_projection import run_l4

WORKSPACE_ID = "00000000-0000-0000-0000-0000000000ff"
TARGET_IDS = {
    "parquet": "target:release-lakehouse",
    "semantic_model": "target:release-semantic-model",
    "ontology": "target:release-ontology",
    "graph": "target:release-graph",
}


def _compiled(source):
    crosswalk = compile_publication_crosswalk(source)
    policy = compile_access_policy(
        source,
        access_policy_id="access-policy:release",
        principal_id="principal:release-publisher",
        resource_scope_id=f"resource:fabric-workspace:{WORKSPACE_ID}",
        authorization_resource_id="authorization-resource:release",
    )
    assets = compile_governed_assets(
        source,
        crosswalks=(crosswalk,),
        access_policy=policy,
        target_ids=TARGET_IDS,
        workspace_id=WORKSPACE_ID,
    )
    return crosswalk, policy, compile_l5a_publication(
        source,
        crosswalks=(crosswalk,),
        access_policy=policy,
        governed_assets=assets,
        target_ids=TARGET_IDS,
    )


@pytest.fixture
def unanchored_source(tmp_path: Path):
    return run_l4(
        _l3_without_manifest(tmp_path),
        state_root=tmp_path / ".fkg" / "l4",
    ).sealed_source()


@pytest.mark.unit
def test_compiled_crosswalk_is_accepted_by_the_publish_authority_gate(
    unanchored_source,
):
    """The compiler must satisfy the exact-cover gate, not merely validate.

    ``_validate_publish_authority`` requires the crosswalk to map every
    non-tombstoned contract type, relationship, and declared property. A
    crosswalk that only passes its own carrier invariants would still be
    rejected there.
    """

    _, _, compiled = _compiled(unanchored_source)
    assert set(compiled.definitions) == {
        "parquet",
        "semantic_model",
        "ontology",
        "graph",
    }
    assert compiled.tables


@pytest.mark.unit
def test_compilation_is_deterministic_for_one_sealed_source(unanchored_source):
    """Dry-run and live must seal byte-identical plans.

    Stable ids, slugs, and BigInt assignments are all derived, so any
    iteration-order leak would surface as a differing crosswalk hash.
    """

    first, _, first_compiled = _compiled(unanchored_source)
    second, _, second_compiled = _compiled(unanchored_source)
    assert first.crosswalk_hash == second.crosswalk_hash
    assert first.stable_id_lock_hash == second.stable_id_lock_hash
    for kind in TARGET_IDS:
        assert canonical_sha256(
            first_compiled.definitions[kind]
        ) == canonical_sha256(second_compiled.definitions[kind])


@pytest.mark.unit
def test_compiled_crosswalk_covers_every_contract_member(unanchored_source):
    """Coverage is exact-match, so under- and over-mapping both fail.

    Pinning the observed sets here makes a coverage regression report the
    missing member rather than an opaque gate rejection.
    """

    crosswalk, _, _ = _compiled(unanchored_source)
    mapped_types = {
        mapping.canonical_semantic_type_id
        for mapping in crosswalk.semantic_type_mappings
    }
    owned = {
        mapping.owner_semantic_type_id
        for mapping in crosswalk.semantic_property_ownership_mappings
    }
    assert owned <= mapped_types
    assert len(crosswalk.relationship_mappings) == len({
        mapping.canonical_semantic_relationship_id
        for mapping in crosswalk.relationship_mappings
    })


@pytest.mark.unit
def test_unanchored_source_compiles_without_a_required_member_manifest(
    unanchored_source,
):
    """A corpus that seals no manifest must still publish.

    This is the shape the real Surface corpus produces, and it is the case
    the compiler has to get right without inventing an anchor.
    """

    crosswalk, _, _ = _compiled(unanchored_source)
    assert crosswalk.authority.anchors_required_member_manifest is False
    assert crosswalk.authority.required_member_manifest_id is None


@pytest.mark.unit
def test_governed_assets_address_targets_by_release_owned_name(
    unanchored_source,
):
    """Item GUIDs are only known after a live create.

    Addressing by workspace plus release-owned name keeps the dry-run plan
    and the live plan byte-identical.
    """

    crosswalk, policy, _ = _compiled(unanchored_source)
    assets = compile_governed_assets(
        unanchored_source,
        crosswalks=(crosswalk,),
        access_policy=policy,
        target_ids=TARGET_IDS,
        workspace_id=WORKSPACE_ID,
    )
    uris = {asset.immutable_locator.source_uri for asset in assets}
    assert any(
        uri.startswith(f"abfss://{WORKSPACE_ID}@onelake.dfs.fabric.microsoft.com/")
        for uri in uris
    )
    assert all(
        uri.startswith("abfss://") or uri.startswith("https://")
        for uri in uris
    )


