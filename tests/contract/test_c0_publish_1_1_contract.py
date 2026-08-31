"""C0.Publish 1.1 inherited-key and physical-binding contract gates."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts import (
    CanonicalIdentityEnvelope,
    EndpointPhysicalKeyBindingV1_1,
    InheritedPropertyReferenceV1_1,
    PhysicalPropertyBindingV1_1,
    PhysicalSurrogateKeyBindingV1_1,
    PublicationAuthorityReferences,
    PublicationCrosswalk,
    PublicationCrosswalkIdentityV1_1,
    PublicationCrosswalkV1_1,
    RelationshipProjectionMappingV1_1,
    SemanticPropertyOwnershipMappingV1_1,
    SemanticTypeProjectionMappingV1_1,
    canonical_json,
    canonical_sha256,
    negotiate_contract,
    parse_contract,
    write_registered_schemas,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "contracts"
SCHEMA_DIR = (
    Path(__file__).parents[2]
    / "src"
    / "fabric_kg_builder"
    / "contracts"
    / "schemas"
)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
REQUIRED_MEMBER_SCHEMA_HASH = (
    "e33003e128746f09c77ba44b4b4802510eadbdf000eb60430f16a4d2654a3c4c"
)


def identity(version: str = "1.1.0") -> PublicationCrosswalkIdentityV1_1:
    return PublicationCrosswalkIdentityV1_1(
        contract_kind="c0.publication_crosswalk",
        contract_version=version,
        project_id="project:generic",
        asset_id=None,
        asset_version_id=None,
        run_id="run:publication-contract-1-1",
        source_file_id=None,
        source_unit_id=None,
        content_hash=None,
        domain_schema_version="2.0",
        domain_contract_hash=HASH_A,
        semantic_contract_hash=HASH_B,
        canonical_schema_version="2.0",
        prompt_version=None,
        prompt_hash=None,
        model_version=None,
        model_hash=None,
        extractor_name=None,
        extractor_version=None,
        parent_artifact_ids=("artifact:asserted",),
        parent_record_ids=(),
        immutable_locator=None,
    )


def authority() -> PublicationAuthorityReferences:
    return PublicationAuthorityReferences(
        required_member_manifest_id="manifest:generic",
        required_member_manifest_contract_version="1.1.0",
        required_member_manifest_schema_hash=REQUIRED_MEMBER_SCHEMA_HASH,
        required_member_manifest_hash=HASH_C,
        authoritative_collection_hash=HASH_D,
        source_artifact_manifest_id="artifact-manifest:asserted",
        source_artifact_manifest_hash=HASH_E,
    )


def ownership(
    canonical_id: str,
    owner_type_id: str,
    suffix: str,
    bigint_id: int,
) -> SemanticPropertyOwnershipMappingV1_1:
    return SemanticPropertyOwnershipMappingV1_1(
        canonical_property_id=canonical_id,
        owner_semantic_type_id=owner_type_id,
        data_type="string",
        value_semantics_id=f"value-semantics:{suffix}",
        ontology_bigint_id=bigint_id,
        graph_property=f"graph-property:{suffix}",
        data_agent_selected_property_id=f"data-agent-property:{suffix}",
    )


def inherited(
    canonical_id: str,
    suffix: str,
) -> InheritedPropertyReferenceV1_1:
    return InheritedPropertyReferenceV1_1(
        canonical_property_id=canonical_id,
        owner_semantic_type_id="semantic-type:root",
        data_type="string",
        value_semantics_id=f"value-semantics:{suffix}",
    )


def binding(
    canonical_id: str,
    owner_type_id: str,
    suffix: str,
    *,
    key: bool = False,
) -> PhysicalPropertyBindingV1_1:
    return PhysicalPropertyBindingV1_1(
        canonical_property_id=canonical_id,
        owner_semantic_type_id=owner_type_id,
        data_type="string",
        value_semantics_id=f"value-semantics:{canonical_id.rsplit(':', 1)[-1]}",
        physical_column_id=f"column:{suffix}",
        search_index_field=f"search-field:{suffix}",
        search_filter_field=f"search-field:{suffix}" if key else None,
        search_vector_field=None if key else f"search-vector:{suffix}",
    )


def type_mapping(
    type_id: str,
    suffix: str,
    bigint_id: int,
    *,
    parent_id: str | None,
    local_ids: tuple[str, ...],
    inherited_refs: tuple[InheritedPropertyReferenceV1_1, ...],
) -> SemanticTypeProjectionMappingV1_1:
    key_ids = ("property:root:id", "property:root:partition")
    effective_ids = (*local_ids, *(item.canonical_property_id for item in inherited_refs))
    bindings = tuple(
        binding(
            canonical_id,
            (
                type_id
                if canonical_id in local_ids
                else "semantic-type:root"
            ),
            f"{suffix}-{canonical_id.rsplit(':', 1)[-1]}",
            key=canonical_id in key_ids,
        )
        for canonical_id in effective_ids
    )
    return SemanticTypeProjectionMappingV1_1(
        canonical_semantic_type_id=type_id,
        canonical_parent_semantic_type_id=parent_id,
        physical_table_id=f"table:{suffix}",
        ontology_bigint_id=bigint_id,
        graph_label=f"Graph{suffix.title()}",
        graph_aliases=(f"Alias{suffix.title()}",),
        locally_owned_canonical_property_ids=local_ids,
        inherited_property_references=inherited_refs,
        canonical_instance_key_property_ids=key_ids,
        physical_property_bindings=bindings,
        physical_surrogate_key_bindings=(
            (
                PhysicalSurrogateKeyBindingV1_1(
                    surrogate_key_id="surrogate:leaf-row-id",
                    physical_column_id="column:leaf-row-id",
                    data_type="integer",
                    purpose="physical_row_identity",
                ),
            )
            if suffix == "leaf"
            else ()
        ),
    )


def endpoint_bindings(
    side: str,
) -> tuple[EndpointPhysicalKeyBindingV1_1, ...]:
    return tuple(
        EndpointPhysicalKeyBindingV1_1(
            canonical_property_id=canonical_id,
            physical_column_id=f"column:{side}-{canonical_id.rsplit(':', 1)[-1]}",
        )
        for canonical_id in ("property:root:id", "property:root:partition")
    )


def publication_crosswalk_v1_1() -> PublicationCrosswalkV1_1:
    root = type_mapping(
        "semantic-type:root",
        "root",
        1001,
        parent_id=None,
        local_ids=(
            "property:root:id",
            "property:root:name",
            "property:root:partition",
        ),
        inherited_refs=(),
    )
    inherited_keys = (
        inherited("property:root:id", "id"),
        inherited("property:root:partition", "partition"),
    )
    intermediate = type_mapping(
        "semantic-type:intermediate",
        "intermediate",
        1002,
        parent_id="semantic-type:root",
        local_ids=("property:intermediate:code",),
        inherited_refs=inherited_keys,
    )
    leaf = type_mapping(
        "semantic-type:leaf",
        "leaf",
        1003,
        parent_id="semantic-type:intermediate",
        local_ids=("property:leaf:value",),
        inherited_refs=inherited_keys,
    )
    relationship = RelationshipProjectionMappingV1_1(
        canonical_semantic_relationship_id="relationship:root-to-leaf",
        source_semantic_type_id="semantic-type:root",
        target_semantic_type_id="semantic-type:leaf",
        physical_table_id="table:root-leaf",
        ontology_bigint_id=3001,
        graph_label="ROOT_TO_LEAF",
        graph_aliases=("ROOT_LEAF",),
        source_canonical_key_property_ids=(
            "property:root:id",
            "property:root:partition",
        ),
        target_canonical_key_property_ids=(
            "property:root:id",
            "property:root:partition",
        ),
        source_key_bindings=endpoint_bindings("source"),
        target_key_bindings=endpoint_bindings("target"),
        search_index_field="search-field:root-leaf",
    )
    values = {
        "identity": identity(),
        "publication_crosswalk_id": "publication-crosswalk:generic-inheritance",
        "authority": authority(),
        "semantic_contract_hash": HASH_B,
        "stable_id_lock_id": "stable-id-lock:generic",
        "stable_id_lock_hash": HASH_E,
        "hierarchy_hash": HASH_F,
        "identity_policy_hash": HASH_A,
        "source_projection_id": "semantic-serving-projection:generic",
        "source_projection_hash": HASH_C,
        "semantic_property_ownership_mappings": (
            ownership(
                "property:intermediate:code",
                "semantic-type:intermediate",
                "code",
                2001,
            ),
            ownership("property:leaf:value", "semantic-type:leaf", "value", 2002),
            ownership("property:root:id", "semantic-type:root", "id", 2003),
            ownership("property:root:name", "semantic-type:root", "name", 2004),
            ownership(
                "property:root:partition",
                "semantic-type:root",
                "partition",
                2005,
            ),
        ),
        "semantic_type_mappings": (intermediate, leaf, root),
        "relationship_mappings": (relationship,),
    }
    return PublicationCrosswalkV1_1(
        **values,
        crosswalk_hash=canonical_sha256(values),
    )


def reseal(payload: dict[str, object]) -> dict[str, object]:
    payload["crosswalk_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "crosswalk_hash"}
    )
    return payload


@pytest.mark.contract
def test_successor_is_additive_and_legacy_reader_is_unchanged() -> None:
    assert negotiate_contract("c0.publication_crosswalk", "1.0.0") is PublicationCrosswalk
    assert (
        negotiate_contract("c0.publication_crosswalk", "1.1.0")
        is PublicationCrosswalkV1_1
    )
    legacy_payload = (
        FIXTURES / "valid" / "publication-crosswalk-clinical.json"
    ).read_text(encoding="utf-8")
    assert isinstance(parse_contract(legacy_payload), PublicationCrosswalk)
    with pytest.raises(ValidationError):
        PublicationCrosswalk.model_validate(
            publication_crosswalk_v1_1().model_dump(mode="json")
        )
    with pytest.raises(Exception, match="major 2 is unsupported"):
        negotiate_contract("c0.publication_crosswalk", "2.0.0")


@pytest.mark.contract
def test_three_level_inherited_keys_and_local_materializations_are_valid() -> None:
    crosswalk = publication_crosswalk_v1_1()
    leaf = crosswalk.semantic_type_mappings[1]
    assert leaf.canonical_semantic_type_id == "semantic-type:leaf"
    assert leaf.locally_owned_canonical_property_ids == ("property:leaf:value",)
    assert leaf.canonical_instance_key_property_ids == (
        "property:root:id",
        "property:root:partition",
    )
    assert {
        item.physical_column_id for item in leaf.physical_property_bindings
    } >= {"column:leaf-id", "column:leaf-partition"}
    assert leaf.physical_surrogate_key_bindings[0].surrogate_key_id == (
        "surrogate:leaf-row-id"
    )
    relationship = crosswalk.relationship_mappings[0]
    assert len(relationship.source_key_bindings) == 2
    assert len(relationship.target_key_bindings) == 2


@pytest.mark.contract
def test_successor_canonicalization_and_hash_are_deterministic() -> None:
    crosswalk = publication_crosswalk_v1_1()
    payload = crosswalk.model_dump(mode="python")
    payload["semantic_type_mappings"] = tuple(
        reversed(payload["semantic_type_mappings"])
    )
    payload["semantic_property_ownership_mappings"] = tuple(
        reversed(payload["semantic_property_ownership_mappings"])
    )
    assert PublicationCrosswalkV1_1.model_validate(payload) == crosswalk


@pytest.mark.contract
@pytest.mark.parametrize(
    ("path", "message"),
    [
        (("semantic_type_mappings", 1, "locally_owned_canonical_property_ids"), "duplicate"),
        (("semantic_type_mappings", 1, "canonical_instance_key_property_ids"), "duplicate"),
        (("relationship_mappings", 0, "source_canonical_key_property_ids"), "duplicate"),
        (
            ("semantic_type_mappings", 0, "locally_owned_canonical_property_ids"),
            "duplicate",
        ),
    ],
)
def test_canonical_set_duplicates_fail_before_normalization(
    path: tuple[object, ...],
    message: str,
) -> None:
    payload = publication_crosswalk_v1_1().model_dump(mode="python")
    target: object = payload
    for part in path:
        target = target[part]
    parent: object = payload
    for part in path[:-1]:
        parent = parent[part]
    duplicate = (
        f"  {target[0]}  "
        if path
        == (
            "semantic_type_mappings",
            0,
            "locally_owned_canonical_property_ids",
        )
        else target[0]
    )
    parent[path[-1]] = (*target, duplicate)
    with pytest.raises(ValidationError, match=message):
        PublicationCrosswalkV1_1.model_validate(payload)


@pytest.mark.contract
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unknown", "inherited canonical property does not resolve"),
        ("owner", "owner or value semantics differ"),
        ("datatype", "owner or value semantics differ"),
        ("semantics", "owner or value semantics differ"),
        ("self", "cannot inherit its own canonical property"),
    ],
)
def test_inherited_references_fail_closed(
    mutation: str,
    message: str,
) -> None:
    payload = publication_crosswalk_v1_1().model_dump(mode="python")
    leaf = copy.deepcopy(payload["semantic_type_mappings"][1])
    reference = leaf["inherited_property_references"][0]
    if mutation == "unknown":
        reference["canonical_property_id"] = "property:unknown"
        next(
            item
            for item in leaf["physical_property_bindings"]
            if item["canonical_property_id"] == "property:root:id"
        )["canonical_property_id"] = "property:unknown"
        leaf["canonical_instance_key_property_ids"] = (
            "property:root:partition",
            "property:unknown",
        )
    elif mutation == "owner":
        reference["owner_semantic_type_id"] = "semantic-type:intermediate"
    elif mutation == "datatype":
        reference["data_type"] = "integer"
    elif mutation == "semantics":
        reference["value_semantics_id"] = "value-semantics:other"
    else:
        reference["canonical_property_id"] = "property:leaf:value"
        reference["owner_semantic_type_id"] = "semantic-type:leaf"
        reference["value_semantics_id"] = "value-semantics:value"
        leaf["locally_owned_canonical_property_ids"] = ()
        leaf["canonical_instance_key_property_ids"] = (
            "property:leaf:value",
            "property:root:partition",
        )
        leaf["physical_property_bindings"] = tuple(
            item
            for item in leaf["physical_property_bindings"]
            if item["canonical_property_id"] != "property:root:id"
        )
    payload["semantic_type_mappings"] = tuple(
        leaf if item["canonical_semantic_type_id"] == "semantic-type:leaf" else item
        for item in payload["semantic_type_mappings"]
    )
    with pytest.raises(ValidationError, match=message):
        PublicationCrosswalkV1_1.model_validate(reseal(payload))


@pytest.mark.contract
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate_owner", "one semantic owner"),
        ("orphan_owner", "owner semantic type does not resolve"),
        ("shadow_owner", "shadows its semantic owner"),
        ("unclaimed_owner", "one local owner claim"),
        ("parent_orphan", "parent semantic type does not resolve"),
        ("parent_cycle", "hierarchy must be acyclic"),
    ],
)
def test_semantic_ownership_and_hierarchy_fail_closed(
    mutation: str,
    message: str,
) -> None:
    payload = publication_crosswalk_v1_1().model_dump(mode="python")
    if mutation == "duplicate_owner":
        payload["semantic_property_ownership_mappings"] += (
            copy.deepcopy(payload["semantic_property_ownership_mappings"][0]),
        )
    elif mutation == "orphan_owner":
        payload["semantic_property_ownership_mappings"][0][
            "owner_semantic_type_id"
        ] = "semantic-type:unknown"
    elif mutation == "shadow_owner":
        payload["semantic_type_mappings"][0][
            "locally_owned_canonical_property_ids"
        ] += ("property:root:name",)
        payload["semantic_type_mappings"][0]["physical_property_bindings"] = (
            binding(
                "property:root:name",
                "semantic-type:root",
                "intermediate-name",
            ).model_dump(mode="python"),
            *payload["semantic_type_mappings"][0]["physical_property_bindings"],
        )
    elif mutation == "unclaimed_owner":
        payload["semantic_property_ownership_mappings"] += (
            ownership(
                "property:orphan",
                "semantic-type:root",
                "orphan",
                2999,
            ).model_dump(mode="python"),
        )
    elif mutation == "parent_orphan":
        payload["semantic_type_mappings"][1][
            "canonical_parent_semantic_type_id"
        ] = "semantic-type:unknown"
    else:
        payload["semantic_type_mappings"][2][
            "canonical_parent_semantic_type_id"
        ] = "semantic-type:leaf"
    with pytest.raises(ValidationError, match=message):
        PublicationCrosswalkV1_1.model_validate(reseal(payload))


@pytest.mark.contract
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "exactly materialize"),
        ("extra", "exactly materialize"),
        ("duplicate", "bindings must be unique"),
        ("owner", "shadows canonical owner"),
        ("datatype", "shadows canonical owner"),
        ("semantics", "shadows canonical owner"),
        ("column_collision", "physical columns must be unique"),
        ("search_collision", "type-local search field"),
        ("surrogate_canonical", "cannot use canonical property IDs"),
        ("surrogate_key", "instance keys must be effective"),
    ],
)
def test_local_property_and_surrogate_bindings_fail_closed(
    mutation: str,
    message: str,
) -> None:
    payload = publication_crosswalk_v1_1().model_dump(mode="python")
    leaf = payload["semantic_type_mappings"][1]
    if mutation == "missing":
        leaf["physical_property_bindings"] = leaf["physical_property_bindings"][1:]
    elif mutation == "extra":
        leaf["physical_property_bindings"] += (
            binding(
                "property:root:name",
                "semantic-type:root",
                "leaf-name",
            ).model_dump(mode="python"),
        )
    elif mutation == "duplicate":
        leaf["physical_property_bindings"] += (
            copy.deepcopy(leaf["physical_property_bindings"][0]),
        )
    elif mutation in {"owner", "datatype", "semantics"}:
        field = {
            "owner": "owner_semantic_type_id",
            "datatype": "data_type",
            "semantics": "value_semantics_id",
        }[mutation]
        leaf["physical_property_bindings"][0][field] = "mismatch"
    elif mutation == "column_collision":
        leaf["physical_surrogate_key_bindings"][0]["physical_column_id"] = (
            leaf["physical_property_bindings"][0]["physical_column_id"]
        )
    elif mutation == "search_collision":
        leaf["physical_property_bindings"][1]["search_index_field"] = (
            leaf["physical_property_bindings"][0]["search_index_field"]
        )
    elif mutation == "surrogate_canonical":
        leaf["physical_surrogate_key_bindings"][0]["surrogate_key_id"] = (
            "property:root:id"
        )
    else:
        leaf["canonical_instance_key_property_ids"] = ("surrogate:leaf-row-id",)
    with pytest.raises(ValidationError, match=message):
        PublicationCrosswalkV1_1.model_validate(reseal(payload))


@pytest.mark.contract
def test_surrogate_ids_cannot_be_ambiguous_across_types() -> None:
    payload = publication_crosswalk_v1_1().model_dump(mode="python")
    payload["semantic_type_mappings"][0]["physical_surrogate_key_bindings"] = (
        {
            "surrogate_key_id": "surrogate:leaf-row-id",
            "physical_column_id": "column:intermediate-row-id",
            "data_type": "integer",
            "purpose": "physical_row_identity",
        },
    )
    with pytest.raises(ValidationError, match="globally unique"):
        PublicationCrosswalkV1_1.model_validate(reseal(payload))


@pytest.mark.contract
def test_global_semantic_namespace_collisions_fail_closed() -> None:
    payload = publication_crosswalk_v1_1().model_dump(mode="python")
    payload["semantic_property_ownership_mappings"][1][
        "data_agent_selected_property_id"
    ] = payload["semantic_property_ownership_mappings"][0][
        "data_agent_selected_property_id"
    ]
    with pytest.raises(ValidationError, match="Data Agent selected property"):
        PublicationCrosswalkV1_1.model_validate(reseal(payload))

    payload = publication_crosswalk_v1_1().model_dump(mode="python")
    relationship = payload["relationship_mappings"][0]
    source_type = payload["semantic_type_mappings"][2]
    relationship["canonical_semantic_relationship_id"] = source_type[
        "canonical_semantic_type_id"
    ]
    relationship["physical_table_id"] = source_type["physical_table_id"]
    relationship["ontology_bigint_id"] = source_type["ontology_bigint_id"]
    relationship["graph_label"] = source_type["graph_label"]
    with pytest.raises(ValidationError, match="canonical IDs must be disjoint"):
        PublicationCrosswalkV1_1.model_validate(reseal(payload))


@pytest.mark.contract
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("wrong_source_keys", "source canonical keys must equal"),
        ("missing_binding", "exactly resolve canonical keys"),
        ("extra_binding", "exactly resolve canonical keys"),
        ("duplicate_binding", "canonical bindings must be unique"),
        ("column_collision", "endpoint physical columns must be unique"),
    ],
)
def test_relationship_endpoint_semantics_and_bindings_fail_closed(
    mutation: str,
    message: str,
) -> None:
    payload = publication_crosswalk_v1_1().model_dump(mode="python")
    relationship = payload["relationship_mappings"][0]
    if mutation == "wrong_source_keys":
        relationship["source_canonical_key_property_ids"] = ("property:root:id",)
        relationship["source_key_bindings"] = relationship["source_key_bindings"][:1]
    elif mutation == "missing_binding":
        relationship["source_key_bindings"] = relationship["source_key_bindings"][:1]
    elif mutation == "extra_binding":
        relationship["source_key_bindings"] += (
            {
                "canonical_property_id": "property:root:name",
                "physical_column_id": "column:source-name",
            },
        )
    elif mutation == "duplicate_binding":
        relationship["source_key_bindings"] += (
            copy.deepcopy(relationship["source_key_bindings"][0]),
        )
    else:
        relationship["target_key_bindings"][0]["physical_column_id"] = (
            relationship["source_key_bindings"][0]["physical_column_id"]
        )
    with pytest.raises(ValidationError, match=message):
        PublicationCrosswalkV1_1.model_validate(reseal(payload))


@pytest.mark.contract
def test_unknown_fields_and_coordinated_reseals_fail_closed() -> None:
    payload = publication_crosswalk_v1_1().model_dump(mode="python")
    payload["semantic_type_mappings"][1]["hierarchy_inference"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PublicationCrosswalkV1_1.model_validate(reseal(payload))

    payload = publication_crosswalk_v1_1().model_dump(mode="python")
    payload["semantic_property_ownership_mappings"][0]["data_type"] = "integer"
    with pytest.raises(ValidationError, match="canonical owner or value semantics"):
        PublicationCrosswalkV1_1.model_validate(reseal(payload))


@pytest.mark.contract
def test_generic_fixture_round_trips_and_matches_golden() -> None:
    parsed = parse_contract(
        (
            FIXTURES
            / "valid"
            / "publication-crosswalk-v1.1-generic-inheritance.json"
        ).read_text(encoding="utf-8")
    )
    assert isinstance(parsed, PublicationCrosswalkV1_1)
    expected_json = (
        FIXTURES
        / "golden"
        / "publication-crosswalk-v1.1-generic-inheritance.canonical.json"
    ).read_text(encoding="utf-8").rstrip("\n")
    expected_hash = (
        FIXTURES
        / "golden"
        / "publication-crosswalk-v1.1-generic-inheritance.sha256"
    ).read_text(encoding="utf-8").strip()
    assert canonical_json(parsed) == expected_json
    assert canonical_sha256(parsed) == expected_hash


@pytest.mark.contract
def test_schema_generation_adds_only_1_1_and_preserves_every_existing_byte(
    tmp_path: Path,
) -> None:
    existing = {
        path.name: path.read_bytes()
        for path in SCHEMA_DIR.glob("*.schema.json")
    }
    write_registered_schemas(tmp_path)
    assert (
        tmp_path / "c0-publication_crosswalk-1.1.0.schema.json"
    ).is_file()
    for name, content in existing.items():
        assert (tmp_path / name).read_bytes() == content

    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    versions = {
        item["contract_version"]
        for item in registry["schemas"]
        if item["contract_kind"] == "c0.publication_crosswalk"
    }
    assert versions == {"1.0.0", "1.1.0", "1.2.0"}
