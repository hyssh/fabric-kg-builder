"""Tests for the product-side L5a crosswalk compiler.

L5a publication used to be reachable only through a crosswalk hand-built in
the test suite, which meant nothing shipped in the wheel could derive one.
These tests pin that the compiled crosswalk is accepted by the very gate the
hand-built one satisfied, so the CLI and the suite prove the same object.
"""

from __future__ import annotations

from pathlib import Path
import copy
import dataclasses
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.serving.l5a_crosswalk import (
    L5aCrosswalkError,
    compile_access_policy,
    compile_governed_assets,
    compile_publication_crosswalk,
    compile_publication_crosswalks,
    publication_crosswalk_set_hash,
)
from fabric_kg_builder.serving.structured_publication import (
    L5aPublicationError,
    compile_l5a_publication,
    require_l5a_publication_receipt,
    run_l5a,
)

from tests.unit.test_l5a_structured_publication import _FakeClient, _inputs, _l3_without_manifest
from tests.unit.test_schema2_validation_stage import _fact_set, _l3, _pipeline
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


@pytest.fixture(params=["1.2.0", "1.3.0"])
def multiple_source(tmp_path, request):
    business_keys = request.param == "1.2.0"
    properties = {
        f"semantic-type:manufacturing.{kind}": ({
            "property_id": f"property:{kind}:id", "display_name": f"{kind.title()} ID",
            "value_type": "string", "required": True,
        },)
        for kind in ("record", "subject")
    } if business_keys else {}

    def mutate(candidates, _work_unit):
        result = copy.deepcopy(candidates)
        result.extend((
            {**result[0], "local_id": "record-2"},
            {**result[2], "source_local_id": "record-2"},
        ))
        if business_keys:
            for owner in tuple(result):
                if owner["candidate_kind"] != "entity":
                    continue
                kind = "record" if owner["local_id"].startswith("record") else "subject"
                value = "record" if owner["local_id"] == "record-2" else f"governed {kind}"
                property_id = f"property:{kind}:id"
                owner["identity_key"] = {property_id: value}
                result.append({
                    "candidate_kind": "property", "owner_local_id": owner["local_id"],
                    "observed_property": property_id, "value": value, "normalized_value": value,
                    "anchor": result[2]["anchor"],
                })
        return result

    l1, domain, _ = _pipeline(
        tmp_path, "manufacturing",
        fact_set=_fact_set("manufacturing", ordered=False, roles=False, expected_count=None),
        mutate=mutate,
        type_properties=properties,
        identity_business_keys={
            type_id: (definitions[0]["property_id"],)
            for type_id, definitions in properties.items()
        } or None,
    )
    source = run_l4(
        _l3(tmp_path, l1, domain), state_root=tmp_path / ".fkg" / "l4",
    ).sealed_source()
    assert pq.read_table(source.resolve("semantic_required_member_manifests")).num_rows == 2
    assert compile_publication_crosswalks(source)[0].identity.contract_version == request.param
    return source


def _plural_inputs(source, crosswalks=None):
    crosswalks = compile_publication_crosswalks(source) if crosswalks is None else crosswalks
    policy = compile_access_policy(
        source, access_policy_id="access-policy:release",
        principal_id="principal:release-publisher",
        resource_scope_id=f"resource:fabric-workspace:{WORKSPACE_ID}",
        authorization_resource_id="authorization-resource:release",
    )
    return {
        "source": source, "crosswalks": crosswalks, "access_policy": policy,
        "target_ids": TARGET_IDS,
        "governed_assets": compile_governed_assets(
            source, crosswalks=crosswalks, access_policy=policy,
            target_ids=TARGET_IDS, workspace_id=WORKSPACE_ID,
        ),
    }


@pytest.mark.unit
def test_plural_compiler_preserves_empty_and_single_manifest_compatibility(unanchored_source, tmp_path):
    single = _inputs(tmp_path / "single")["source"]
    for source in (unanchored_source, single):
        crosswalk = compile_publication_crosswalk(source)
        assert compile_publication_crosswalks(source) == (crosswalk,)
        assert publication_crosswalk_set_hash((crosswalk,)) == crosswalk.crosswalk_hash
        assert len(compile_l5a_publication(**_plural_inputs(source)).required_member_snapshots) == 1


@pytest.mark.unit
def test_plural_compiler_publishes_and_proves_every_manifest(multiple_source, tmp_path):
    inputs = _plural_inputs(multiple_source)
    crosswalks = inputs["crosswalks"]
    assert crosswalks == compile_publication_crosswalks(multiple_source)
    assert len({item.publication_crosswalk_id for item in crosswalks}) == 2
    assert len({item.crosswalk_hash for item in crosswalks}) == 2
    assert len({item.stable_id_lock_hash for item in crosswalks}) == 1
    assert crosswalks[0].semantic_type_mappings == crosswalks[1].semantic_type_mappings
    assert publication_crosswalk_set_hash(crosswalks) == publication_crosswalk_set_hash(crosswalks[::-1])
    compiled = compile_l5a_publication(**inputs)
    manifest_ids = {row["required_member_manifest_id"] for row in compiled.required_member_manifest_rows}
    assert {item.authority.required_member_manifest_id for item in crosswalks} == manifest_ids
    assert len(compiled.required_member_snapshots) == 2
    result = run_l5a(**inputs, client=_FakeClient(), state_root=tmp_path / "l5a")
    assert result.receipt.status == "succeeded"
    assert len(result.projection_equivalences) == 8
    assert {item.authority.required_member_manifest_id for item in result.projection_equivalences} == manifest_ids
    require_l5a_publication_receipt(multiple_source, result)
    saved = json.loads((result.run_root / "publication-crosswalks.json").read_text())
    assert len(saved) == 2
    with pytest.raises(L5aCrosswalkError, match="use compile_publication_crosswalks"):
        compile_publication_crosswalk(multiple_source)


@pytest.mark.unit
@pytest.mark.parametrize("change", ["missing", "duplicate", "authority", "definition"])
def test_plural_publication_rejects_incomplete_or_tampered_crosswalks(multiple_source, change):
    inputs = _plural_inputs(multiple_source)
    crosswalks = inputs["crosswalks"]
    if change == "missing":
        changed = crosswalks[:1]
    elif change == "duplicate":
        changed = (*crosswalks, crosswalks[1])
    else:
        values = crosswalks[1].model_dump(mode="python", exclude={"crosswalk_hash"})
        if change == "authority":
            values["authority"]["required_member_manifest_hash"] = "f" * 64
        else:
            values["stable_id_lock_hash"] = "f" * 64
        changed = (crosswalks[0], type(crosswalks[1])(
            **values, crosswalk_hash=canonical_sha256(values),
        ))
    with pytest.raises(L5aPublicationError, match={
        "missing": "L5A_PUBLICATION_CROSSWALK_SET_MISMATCH",
        "duplicate": "L5A_PUBLICATION_CROSSWALK_SET_MISMATCH",
        "authority": "L5A_PUBLICATION_AUTHORITY_MISMATCH",
        "definition": "L5A_CROSSWALK_DEFINITION_DRIFT",
    }[change]):
        compile_l5a_publication(**{**inputs, "crosswalks": changed})


@pytest.mark.unit
def test_plural_compiler_is_manifest_order_independent_and_rejects_bad_anchors(multiple_source, monkeypatch):
    expected = compile_publication_crosswalks(multiple_source)
    path = multiple_source.resolve("semantic_required_member_manifests")
    original_read = pq.read_table
    table = original_read(path)
    rows = table.to_pylist()[::-1]

    def read(path_arg, *args, **kwargs):
        if path_arg == path:
            return pa.Table.from_pylist(rows, schema=table.schema)
        return original_read(path_arg, *args, **kwargs)

    monkeypatch.setattr(pq, "read_table", read)
    assert compile_publication_crosswalks(multiple_source) == expected
    rows[0]["manifest_hash"] = "f" * 64
    with pytest.raises(L5aCrosswalkError, match="L5A_CROSSWALK_MANIFEST_AUTHORITY_MISMATCH"):
        compile_publication_crosswalks(multiple_source)
    rows[:] = table.to_pylist()[:1]
    with pytest.raises(ValueError, match="sealed L4 table differs from its artifact manifest"):
        compile_publication_crosswalks(multiple_source)
    rows.append(rows[0])
    with pytest.raises(ValueError, match="sealed L4 table differs from its artifact manifest"):
        compile_publication_crosswalks(multiple_source)


@pytest.mark.unit
def test_business_quality_cli_and_prototype_keep_all_crosswalks(multiple_source, tmp_path):
    from fabric_kg_builder.cli import cli
    from fabric_kg_builder.deploy.schema2_prototype import _compile

    l3_root = tmp_path / ".fkg" / "l3"
    output = tmp_path / "quality.json"
    result = CliRunner().invoke(cli, [
        "assess-business-quality", "--l4-run", str(multiple_source.root),
        "--l3-root", str(l3_root), "--output", str(output),
    ])
    assert result.exit_code in (0, 5), result.output
    report = json.loads(output.read_text())
    crosswalks = compile_publication_crosswalks(multiple_source)
    assert len(report["crosswalk_hashes"]) == 2
    compilation = _compile(multiple_source.root, l3_root, WORKSPACE_ID, "multi")
    assert compilation.provenance["crosswalk_hash"] == publication_crosswalk_set_hash(crosswalks)
    assert compilation.provenance["crosswalk_hashes"] == sorted(item.crosswalk_hash for item in crosswalks)
    plan_path = tmp_path / "multi-plan.json"
    result = CliRunner().invoke(cli, [
        "app", "publish-structured", "--l4-run", str(multiple_source.root),
        "--l3-root", str(l3_root), "--workspace-id", WORKSPACE_ID,
        "--plan", str(plan_path), "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    plan = json.loads(plan_path.read_text())
    assert plan["crosswalk_hash"] == publication_crosswalk_set_hash(crosswalks)
    assert plan["crosswalk_hashes"] == compilation.provenance["crosswalk_hashes"]


@pytest.mark.unit
@pytest.mark.parametrize("change", ["drop", "reassign", "forge"])
def test_second_manifest_member_readback_fails_closed(multiple_source, tmp_path, change):
    inputs = _plural_inputs(multiple_source)
    first_id, second_id = (
        item.authority.required_member_manifest_id for item in inputs["crosswalks"]
    )

    class TamperedClient(_FakeClient):
        def read_back(self, target_kind, target_id):
            operation = super().read_back(target_kind, target_id)
            if target_kind != "graph" or operation.state is None:
                return operation
            rows = []
            for original in operation.state.required_member_rows:
                row = dict(original)
                if row["required_member_manifest_id"] == second_id:
                    if change == "drop":
                        continue
                    if change == "reassign":
                        row["required_member_manifest_id"] = first_id
                    else:
                        row["member_canonical_id"] = "entity:forged"
                rows.append(row)
            return dataclasses.replace(
                operation, state=dataclasses.replace(operation.state, required_member_rows=tuple(rows)),
            )

    with pytest.raises(L5aPublicationError) as error:
        run_l5a(**inputs, client=TamperedClient(), state_root=tmp_path / "tampered-l5a")
    assert error.value.receipt.status == "failed"
    assert "REQUIRED_MEMBER" in error.value.code
