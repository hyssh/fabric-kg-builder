"""Behavior-free C0.Publish contract and registry gate."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts import (
    AccessPolicy,
    CanonicalIdentityEnvelope,
    EndpointKeyProjectionMapping,
    GovernedAssetReference,
    ImmutableSourceLocator,
    PrincipalScope,
    ProjectionEquivalence,
    ProjectionEvidence,
    PropertyProjectionMapping,
    PublicationAuthorityReferences,
    PublicationCrosswalk,
    RelationshipProjectionMapping,
    RequiredMemberManifestV1_1,
    SemanticTypeProjectionMapping,
    StorageReference,
    UnknownContractKindError,
    canonical_json,
    canonical_sha256,
    negotiate_contract,
    parse_contract,
    write_registered_schemas,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "contracts"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
REQUIRED_MEMBER_SCHEMA_HASH = (
    "e33003e128746f09c77ba44b4b4802510eadbdf000eb60430f16a4d2654a3c4c"
)
RUNTIME_CONTRACT_KINDS = {
    "c0.query_budget",
    "c0.ontology_scope_envelope",
    "c0.resolved_ontology_scope",
    "c0.resolved_retrieval_scope",
    "c0.agentic_retrieval_request_context",
    "c0.agentic_retrieval_coverage_receipt",
    "c0.search_citation_envelope",
    "c0.citation_presentation",
}
FROZEN_EXISTING_SCHEMA_HASHES = {
    ("c0.artifact_manifest", "1.0.0"): (
        "d6337f29de3bc304a9426c385095a6c7f19d14ff7474f1113cf9efdd55670d1b"
    ),
    ("c0.audit_projection", "1.0.0"): (
        "07dddf02357436c6ef039f257381437196e9ae5354310eb1e21b90ab1e3e82ef"
    ),
    ("c0.candidate_accounting_disposition", "1.0.0"): (
        "4091fc91be4702e42b9c6d8d71b11e887d40e885b46adda3b5756d58a81a6021"
    ),
    ("c0.candidate_lifecycle_record", "1.0.0"): (
        "e405ca0e84bb532092f79fd5db470a5098cd5b674e15f5f6940950506ea3d4b8"
    ),
    ("c0.canonical_entity_assertion", "1.0.0"): (
        "e4da9f1d82da83695e18b6289798e7dc0fc6f494366e1a1e96b9951387a97ef9"
    ),
    ("c0.canonical_property_assertion", "1.0.0"): (
        "c05a9b1aaafa02ead69fa8a7d13020ec8fb2ce2c865362d190333f13985b8d37"
    ),
    ("c0.canonical_relationship_assertion", "1.0.0"): (
        "e0f7b85eb42fde74be70adca81358507e9892dfe2082aecb3a0e23ca12811bcf"
    ),
    ("c0.evidence_span", "1.0.0"): (
        "8772f82c1ff467ccb45fb9be6e23c3ecfc596f286050a30a6107c73837854297"
    ),
    ("c0.evidence_span", "1.1.0"): (
        "52a190ca945365ff93c654fb7dd2a7c4fd309b97293080b371892271d5361aa8"
    ),
    ("c0.extraction_candidate_batch", "1.0.0"): (
        "30fa8b04eea261f2efed9e58ccdfaaea04ed330f426c5582a1fae81acda9ce9e"
    ),
    ("c0.identity", "1.0.0"): (
        "74ef5dadcf6d559610cf080ce1471b503e93796a82d555e12813e5e0556f8fc5"
    ),
    ("c0.required_member_manifest", "1.0.0"): (
        "a7cf633c54f3139485fee2e9abafc43727a3ff32627603d971a721d4ba1aff1f"
    ),
    ("c0.required_member_manifest", "1.1.0"): REQUIRED_MEMBER_SCHEMA_HASH,
    ("c0.required_member_set_proposal", "1.0.0"): (
        "5a836f5f1e2aae600785a6bf4c1cd3b9ea3879aeab2712df31c41dc1539e6476"
    ),
    ("c0.required_member_set_proposal", "1.1.0"): (
        "dbb2bccc62bade0911bc6d4f031846af9c4de780926ace5b42383ae1a5ca6cc3"
    ),
    ("c0.semantic_serving_projection", "1.0.0"): (
        "cc078c2761e6a1954aee101b874bb2d08346ffce3205b172a8f5ffa5dd676457"
    ),
    ("c0.source_unit", "1.0.0"): (
        "1a8468ca429683aa137de4b9cd81c0ecbcce3a9b3e1f3f14834a9dc83df58938"
    ),
    ("c0.stage_receipt", "1.0.0"): (
        "05e4f248d5ef6b8ece939cbb593a091c00eafb80100dfc42ffe4297cb56133ce"
    ),
    ("c0.stage_resource_metrics", "1.0.0"): (
        "b8e986f8797ac5d48cee015c9b75d79f63caed1cd0b81c060a6dc1a205729bc8"
    ),
}


def identity(
    kind: str,
    *,
    project_id: str = "project:clinical",
    source: bool = False,
) -> CanonicalIdentityEnvelope:
    locator = immutable_locator() if source else None
    return CanonicalIdentityEnvelope(
        contract_kind=kind,
        contract_version="1.0.0",
        project_id=project_id,
        asset_id="asset:document" if source else None,
        asset_version_id="asset-version:document:1" if source else None,
        run_id="run:publish-contract",
        source_file_id="source-file:document" if source else None,
        source_unit_id=None,
        content_hash=HASH_A if source else None,
        domain_schema_version="2.0",
        domain_contract_hash=HASH_B,
        semantic_contract_hash=HASH_C,
        canonical_schema_version="2.0",
        prompt_version=None,
        prompt_hash=None,
        model_version=None,
        model_hash=None,
        extractor_name=None,
        extractor_version=None,
        parent_artifact_ids=("artifact:source",),
        parent_record_ids=(),
        immutable_locator=locator,
    )


def immutable_locator() -> ImmutableSourceLocator:
    values = {
        "locator_version": "1.0",
        "blob_uri": "https://storage.example.test/source/document.pdf",
        "blob_version_id": "version:document:1",
        "source_uri": None,
        "page": 0,
        "sheet": None,
        "slide": None,
        "section_path": ("overview",),
        "cell_range": None,
        "char_start": None,
        "char_end": None,
        "polygon": None,
        "sheet_zone": None,
        "tile_id": None,
        "coordinate_system": None,
        "transform": None,
        "native_layer_id": None,
        "native_object_id": None,
    }
    return ImmutableSourceLocator(**values, locator_hash=canonical_sha256(values))


def publication_authority() -> PublicationAuthorityReferences:
    return PublicationAuthorityReferences(
        required_member_manifest_id="manifest:c0-1-1",
        required_member_manifest_contract_version="1.1.0",
        required_member_manifest_schema_hash=REQUIRED_MEMBER_SCHEMA_HASH,
        required_member_manifest_hash=(
            "9a1e0ebf33b4e79148639732641604433dd41316f1bbf400142bd5c9c7bf8539"
        ),
        authoritative_collection_hash=(
            "ec9d7585e9f30a8191757139da678ac377aaacb92792fdc673554ef185b33ee8"
        ),
        source_artifact_manifest_id="artifact-manifest:asserted",
        source_artifact_manifest_hash=HASH_D,
    )


def property_mapping(
    canonical_id: str,
    suffix: str,
    bigint_id: int,
    *,
    instance_key: bool = False,
) -> PropertyProjectionMapping:
    return PropertyProjectionMapping(
        canonical_property_id=canonical_id,
        physical_column_id=f"column:{suffix}",
        ontology_bigint_id=bigint_id,
        graph_property=f"graph-property:{suffix}",
        search_index_field=f"search-field:{suffix}",
        search_filter_field=f"search-field:{suffix}" if instance_key else None,
        search_vector_field=None if instance_key else f"search-vector:{suffix}",
        data_agent_selected_property_id=f"data-agent-property:{suffix}",
    )


def type_mapping(
    canonical_id: str,
    suffix: str,
    bigint_id: int,
    property_bigint_id: int,
) -> SemanticTypeProjectionMapping:
    key_id = f"property:{suffix}:id"
    return SemanticTypeProjectionMapping(
        canonical_semantic_type_id=canonical_id,
        canonical_parent_semantic_type_id=None,
        physical_table_id=f"table:{suffix}",
        ontology_bigint_id=bigint_id,
        graph_label=f"Graph{suffix.title()}",
        graph_aliases=(f"Alias{suffix.title()}",),
        canonical_instance_key_property_ids=(key_id,),
        property_mappings=(
            property_mapping(key_id, f"{suffix}-id", property_bigint_id, instance_key=True),
        ),
    )


def publication_crosswalk(
    *,
    project_id: str = "project:clinical",
) -> PublicationCrosswalk:
    patient = type_mapping("semantic-type:patient", "patient", 1001, 2001)
    encounter = type_mapping("semantic-type:encounter", "encounter", 1002, 2002)
    relationship = RelationshipProjectionMapping(
        canonical_semantic_relationship_id="relationship:patient-has-encounter",
        source_semantic_type_id="semantic-type:patient",
        target_semantic_type_id="semantic-type:encounter",
        physical_table_id="table:patient-encounter",
        ontology_bigint_id=3001,
        graph_label="HAS_ENCOUNTER",
        graph_aliases=("PATIENT_ENCOUNTER",),
        source_key_fields=(
            EndpointKeyProjectionMapping(
                canonical_property_id="property:patient:id",
                physical_column_id="column:patient-ref",
            ),
        ),
        target_key_fields=(
            EndpointKeyProjectionMapping(
                canonical_property_id="property:encounter:id",
                physical_column_id="column:encounter-ref",
            ),
        ),
        search_index_field="search-field:patient-encounter",
    )
    values = {
        "identity": identity("c0.publication_crosswalk", project_id=project_id),
        "publication_crosswalk_id": "publication-crosswalk:clinical",
        "authority": publication_authority(),
        "semantic_contract_hash": HASH_C,
        "stable_id_lock_id": "stable-id-lock:clinical",
        "stable_id_lock_hash": HASH_D,
        "hierarchy_hash": HASH_E,
        "identity_policy_hash": HASH_F,
        "source_projection_id": "semantic-serving-projection:clinical",
        "source_projection_hash": HASH_A,
        "semantic_type_mappings": (encounter, patient),
        "relationship_mappings": (relationship,),
    }
    return PublicationCrosswalk(
        **values,
        crosswalk_hash=canonical_sha256(values),
    )


def projection_evidence(
    projection_kind: str,
    *,
    count: int = 3,
    id_set_hash: str = HASH_A,
    fingerprint: str = HASH_B,
) -> ProjectionEvidence:
    values = {
        "count": count,
        "canonical_id_set_hash": id_set_hash,
        "row_fingerprint": None,
        "definition_fingerprint": None,
        "index_fingerprint": None,
    }
    fingerprint_field = {
        "parquet": "row_fingerprint",
        "semantic_model": "definition_fingerprint",
        "ontology": "definition_fingerprint",
        "graph": "definition_fingerprint",
        "search": "index_fingerprint",
    }[projection_kind]
    values[fingerprint_field] = fingerprint
    return ProjectionEvidence(**values)


def projection_equivalence(
    projection_kind: str = "graph",
    *,
    project_id: str = "project:supply-chain",
) -> ProjectionEquivalence:
    crosswalk = publication_crosswalk(project_id=project_id)
    evidence = projection_evidence(projection_kind)
    values = {
        "identity": identity("c0.projection_equivalence", project_id=project_id),
        "projection_equivalence_id": f"projection-equivalence:{projection_kind}",
        "authority": crosswalk.authority,
        "publication_crosswalk_id": crosswalk.publication_crosswalk_id,
        "publication_crosswalk_hash": crosswalk.crosswalk_hash,
        "source_projection_id": crosswalk.source_projection_id,
        "source_projection_hash": crosswalk.source_projection_hash,
        "projection_kind": projection_kind,
        "expected": evidence,
        "compiled": evidence,
        "deployed": evidence,
        "read_back": evidence,
        "missing_canonical_ids": (),
        "extra_canonical_ids": (),
        "equivalent": True,
    }
    return ProjectionEquivalence(
        **values,
        equivalence_hash=canonical_sha256(values),
    )


def access_policy(
    *,
    project_id: str = "project:logistics",
    short_lived_url: bool = True,
) -> AccessPolicy:
    values = {
        "identity": identity("c0.access_policy", project_id=project_id),
        "access_policy_id": "access-policy:delivery",
        "principal_scopes": (
            PrincipalScope(
                principal_type="group",
                principal_id="principal:reviewers",
                resource_scope_ids=("resource:delivery-assets",),
            ),
        ),
        "allowed_operations": (
            ("content", "metadata", "short_lived_url")
            if short_lived_url
            else ("content", "metadata")
        ),
        "sensitivity": "confidential",
        "retention_class": "retention:seven-years",
        "retain_until_utc": "2033-08-24T00:00:00Z",
        "legal_hold": False,
        "legal_hold_reference": None,
        "authorization_resource_id": "authorization-resource:delivery",
    }
    return AccessPolicy(**values, policy_hash=canonical_sha256(values))


def storage_reference() -> StorageReference:
    values = {
        "storage_kind": "azure_blob",
        "storage_account_resource_id": "resource:storage-account",
        "container_id": "container:published-assets",
        "object_id": "object:document.pdf",
        "object_version_id": "object-version:1",
    }
    return StorageReference(
        **values,
        storage_reference_hash=canonical_sha256(values),
    )


def governed_asset(
    asset_kind: str = "original",
    *,
    project_id: str = "project:research",
) -> GovernedAssetReference:
    policy = access_policy(project_id=project_id)
    source_identity = identity(
        "c0.governed_asset_reference",
        project_id=project_id,
        source=True,
    )
    values = {
        "identity": source_identity,
        "governed_asset_reference_id": f"governed-asset:{asset_kind}",
        "asset_kind": asset_kind,
        "source_file_id": source_identity.source_file_id,
        "asset_id": source_identity.asset_id,
        "asset_version_id": source_identity.asset_version_id,
        "immutable_locator": source_identity.immutable_locator,
        "content_hash": source_identity.content_hash,
        "storage_reference": storage_reference(),
        "access_policy_id": policy.access_policy_id,
        "access_policy_hash": policy.policy_hash,
        "on_demand_url_policy": "authorized_short_lived",
    }
    return GovernedAssetReference(
        **values,
        asset_reference_hash=canonical_sha256(values),
    )


@pytest.mark.contract
def test_four_publish_kinds_retain_1_0_with_additive_crosswalk_successor() -> None:
    expected = {
        "c0.publication_crosswalk": PublicationCrosswalk,
        "c0.projection_equivalence": ProjectionEquivalence,
        "c0.governed_asset_reference": GovernedAssetReference,
        "c0.access_policy": AccessPolicy,
    }
    for kind, model in expected.items():
        assert negotiate_contract(kind, "1.0.0") is model
        if kind != "c0.publication_crosswalk":
            with pytest.raises(ValueError, match="not registered"):
                negotiate_contract(kind, "1.1.0")
    assert {
        kind
        for kind in (
            "c0.publication_crosswalk",
            "c0.projection_equivalence",
            "c0.governed_asset_reference",
            "c0.access_policy",
        )
        if negotiate_contract(kind, "1.0.0")
    } == set(expected)


@pytest.mark.contract
def test_publish_contracts_are_strict_frozen_and_hash_bound() -> None:
    for contract, hash_field in (
        (publication_crosswalk(), "crosswalk_hash"),
        (projection_equivalence(), "equivalence_hash"),
        (governed_asset(), "asset_reference_hash"),
        (access_policy(), "policy_hash"),
    ):
        payload = contract.model_dump(mode="json")
        payload["unknown_field"] = "rejected"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            type(contract).model_validate(payload)
        with pytest.raises(ValidationError):
            setattr(contract, hash_field, HASH_A)
        with pytest.raises(ValidationError, match=hash_field):
            contract.model_copy(update={hash_field: HASH_A})


@pytest.mark.contract
def test_crosswalk_maps_all_publication_namespaces_and_canonical_keys() -> None:
    crosswalk = publication_crosswalk()
    assert len(crosswalk.semantic_type_mappings) == 2
    patient = crosswalk.semantic_type_mappings[1]
    assert patient.canonical_instance_key_property_ids == ("property:patient:id",)
    assert patient.property_mappings[0].physical_column_id == "column:patient-id"
    assert patient.property_mappings[0].ontology_bigint_id == 2001
    assert patient.property_mappings[0].graph_property == "graph-property:patient-id"
    assert patient.property_mappings[0].search_filter_field
    assert patient.property_mappings[0].data_agent_selected_property_id
    relationship = crosswalk.relationship_mappings[0]
    assert relationship.source_key_fields[0].canonical_property_id
    assert relationship.target_key_fields[0].physical_column_id


@pytest.mark.contract
@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("ontology_bigint_id", 1002, "ontology BigInt"),
        ("graph_label", "GraphEncounter", "graph label"),
        ("physical_table_id", "table:encounter", "physical table"),
    ],
)
def test_crosswalk_rejects_physical_id_reuse(
    field: str,
    replacement: object,
    message: str,
) -> None:
    crosswalk = publication_crosswalk()
    payload = crosswalk.model_dump(mode="python")
    second = crosswalk.semantic_type_mappings[1]
    changed = second.model_copy(update={field: replacement})
    payload["semantic_type_mappings"] = (
        crosswalk.semantic_type_mappings[0],
        changed,
    )
    payload["crosswalk_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "crosswalk_hash"}
    )
    with pytest.raises(ValidationError, match=message):
        PublicationCrosswalk.model_validate(payload)


@pytest.mark.contract
def test_crosswalk_rejects_key_reuse_and_stale_upstream_hashes() -> None:
    crosswalk = publication_crosswalk()
    payload = crosswalk.model_dump(mode="python")
    second_type = crosswalk.semantic_type_mappings[1]
    second_property = second_type.property_mappings[0].model_copy(
        update={"canonical_property_id": "property:encounter:id"}
    )
    payload["semantic_type_mappings"] = (
        crosswalk.semantic_type_mappings[0],
        second_type.model_copy(
            update={
                "canonical_instance_key_property_ids": ("property:encounter:id",),
                "property_mappings": (second_property,),
            }
        ),
    )
    payload["crosswalk_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "crosswalk_hash"}
    )
    with pytest.raises(ValidationError, match="globally unique"):
        PublicationCrosswalk.model_validate(payload)

    with pytest.raises(ValueError, match="stale hierarchy"):
        crosswalk.validate_upstream_authority(
            hierarchy_hash=HASH_A,
            identity_policy_hash=crosswalk.identity_policy_hash,
            stable_id_lock_hash=crosswalk.stable_id_lock_hash,
            source_projection_hash=crosswalk.source_projection_hash,
        )


@pytest.mark.contract
def test_required_member_manifest_reference_is_exact_and_never_recomputed() -> None:
    manifest = parse_contract(
        (
            FIXTURES / "valid" / "required-member-manifest-v1.1-logistics.json"
        ).read_text(encoding="utf-8")
    )
    assert isinstance(manifest, RequiredMemberManifestV1_1)
    authority = publication_authority()
    authority.validate_required_member_manifest(
        manifest,
        schema_hash=REQUIRED_MEMBER_SCHEMA_HASH,
    )

    with pytest.raises(ValueError, match="schema hash mismatch"):
        authority.validate_required_member_manifest(manifest, schema_hash=HASH_A)
    changed = authority.model_copy(
        update={"required_member_manifest_hash": HASH_A}
    )
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        changed.validate_required_member_manifest(
            manifest,
            schema_hash=REQUIRED_MEMBER_SCHEMA_HASH,
        )
    with pytest.raises(ValidationError):
        PublicationAuthorityReferences.model_validate(
            {
                **authority.model_dump(mode="json"),
                "required_member_manifest_contract_version": "1.0.0",
            }
        )
    authority.validate_source_artifact_manifest(
        SimpleNamespace(
            artifact_manifest_id=authority.source_artifact_manifest_id,
            manifest_hash=authority.source_artifact_manifest_hash,
        )
    )
    with pytest.raises(ValueError, match="source artifact manifest hash mismatch"):
        authority.validate_source_artifact_manifest(
            SimpleNamespace(
                artifact_manifest_id=authority.source_artifact_manifest_id,
                manifest_hash=HASH_A,
            )
        )


@pytest.mark.contract
@pytest.mark.parametrize(
    "projection_kind",
    ["parquet", "semantic_model", "ontology", "graph", "search"],
)
def test_equivalence_requires_exact_counts_id_sets_and_fingerprints(
    projection_kind: str,
) -> None:
    proof = projection_equivalence(projection_kind)
    assert proof.equivalent is True
    assert proof.expected == proof.compiled == proof.deployed == proof.read_back

    for field, changed_evidence in (
        ("compiled", projection_evidence(projection_kind, count=4)),
        ("deployed", projection_evidence(projection_kind, id_set_hash=HASH_C)),
        ("read_back", projection_evidence(projection_kind, fingerprint=HASH_D)),
    ):
        payload = proof.model_dump(mode="python")
        payload[field] = changed_evidence
        payload["equivalence_hash"] = canonical_sha256(
            {key: value for key, value in payload.items() if key != "equivalence_hash"}
        )
        with pytest.raises(ValidationError, match="exact count"):
            ProjectionEquivalence.model_validate(payload)


@pytest.mark.contract
def test_equivalence_rejects_missing_extra_and_crosswalk_hash_mismatch() -> None:
    proof = projection_equivalence()
    for field in ("missing_canonical_ids", "extra_canonical_ids"):
        payload = proof.model_dump(mode="python")
        payload[field] = ("canonical:missing",)
        payload["equivalence_hash"] = canonical_sha256(
            {key: value for key, value in payload.items() if key != "equivalence_hash"}
        )
        with pytest.raises(ValidationError, match="missing or extra"):
            ProjectionEquivalence.model_validate(payload)

    crosswalk = publication_crosswalk(project_id="project:supply-chain")
    proof.validate_crosswalk(crosswalk)
    payload = proof.model_dump(mode="python")
    payload["publication_crosswalk_hash"] = HASH_A
    payload["equivalent"] = False
    payload["equivalence_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "equivalence_hash"}
    )
    changed = ProjectionEquivalence.model_validate(payload)
    with pytest.raises(ValueError, match="crosswalk hash mismatch"):
        changed.validate_crosswalk(crosswalk)


@pytest.mark.contract
@pytest.mark.parametrize(
    "asset_kind",
    ["original", "visual", "table", "derived", "other"],
)
def test_generic_governed_assets_are_authorized(asset_kind: str) -> None:
    asset = governed_asset(asset_kind)
    policy = access_policy(project_id="project:research")
    asset.validate_access_policy(policy)
    assert asset.storage_reference.object_version_id
    assert asset.immutable_locator.locator_hash


@pytest.mark.contract
def test_asset_url_policy_requires_authorization_but_contains_no_url() -> None:
    asset = governed_asset()
    policy = access_policy(project_id="project:research", short_lived_url=False)
    payload = asset.model_dump(mode="python")
    payload["access_policy_hash"] = policy.policy_hash
    payload["asset_reference_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "asset_reference_hash"
        }
    )
    asset = GovernedAssetReference.model_validate(payload)
    with pytest.raises(ValueError, match="does not allow"):
        asset.validate_access_policy(policy)
    schema = GovernedAssetReference.model_json_schema()
    serialized = json.dumps(schema).lower()
    assert "signed_url" not in serialized
    assert "sas" not in serialized
    assert "bearer" not in serialized


@pytest.mark.contract
def test_text_citations_remain_independent_of_governed_assets() -> None:
    text_citation = {
        "citation_kind": "text",
        "source_unit_id": "source-unit:paragraph",
        "quote": "Exact text citation.",
    }
    assert "governed_asset_reference_id" not in text_citation
    with pytest.raises(UnknownContractKindError):
        negotiate_contract("c0.citation", "1.0.0")


@pytest.mark.contract
@pytest.mark.parametrize(
    ("factory", "path", "secret"),
    [
        (storage_reference, ("object_id",), "object.pdf?sig=secret"),
        (access_policy, ("authorization_resource_id",), "Bearer abc.def"),
        (governed_asset, ("access_policy_id",), "policy?token=secret"),
    ],
)
def test_publish_contracts_reject_secrets(
    factory: object,
    path: tuple[str, ...],
    secret: str,
) -> None:
    model = factory()
    payload = model.model_dump(mode="python")
    payload[path[0]] = secret
    hash_field = {
        StorageReference: "storage_reference_hash",
        AccessPolicy: "policy_hash",
        GovernedAssetReference: "asset_reference_hash",
    }[type(model)]
    payload[hash_field] = canonical_sha256(
        {key: value for key, value in payload.items() if key != hash_field}
    )
    with pytest.raises(ValidationError, match="credentials or signed tokens"):
        type(model).model_validate(payload)


@pytest.mark.contract
def test_publish_hashes_are_deterministic_under_set_ordering() -> None:
    policy = access_policy()
    payload = policy.model_dump(mode="python")
    payload["allowed_operations"] = tuple(reversed(payload["allowed_operations"]))
    payload["policy_hash"] = policy.policy_hash
    assert AccessPolicy.model_validate(payload) == policy

    crosswalk = publication_crosswalk()
    payload = crosswalk.model_dump(mode="python")
    payload["semantic_type_mappings"] = tuple(
        reversed(payload["semantic_type_mappings"])
    )
    payload["crosswalk_hash"] = crosswalk.crosswalk_hash
    assert PublicationCrosswalk.model_validate(payload) == crosswalk


@pytest.mark.contract
def test_multiple_domain_fixtures_round_trip_and_match_goldens() -> None:
    fixture_names = (
        "publication-crosswalk-clinical",
        "projection-equivalence-supply-chain",
        "governed-asset-reference-research",
        "access-policy-logistics",
    )
    projects = set()
    for name in fixture_names:
        parsed = parse_contract(
            (FIXTURES / "valid" / f"{name}.json").read_text(encoding="utf-8")
        )
        projects.add(parsed.identity.project_id)
        expected_json = (
            FIXTURES / "golden" / f"{name}.canonical.json"
        ).read_text(encoding="utf-8").rstrip("\n")
        expected_hash = (
            FIXTURES / "golden" / f"{name}.sha256"
        ).read_text(encoding="utf-8").strip()
        assert canonical_json(parsed) == expected_json
        assert canonical_sha256(parsed) == expected_hash
        assert parse_contract(expected_json) == parsed
    assert projects == {
        "project:clinical",
        "project:supply-chain",
        "project:research",
        "project:logistics",
    }


@pytest.mark.contract
def test_publish_schemas_are_generated_and_existing_hashes_are_unchanged(
    tmp_path: Path,
) -> None:
    previous_registry = json.loads(
        (
            Path(__file__).parents[2]
            / "src"
            / "fabric_kg_builder"
            / "contracts"
            / "schemas"
            / "registry.json"
        ).read_text(encoding="utf-8")
    )
    previous_hashes = {
        (item["contract_kind"], item["contract_version"]): item["schema_hash"]
        for item in previous_registry["schemas"]
        if item["contract_kind"] not in {
            "c0.publication_crosswalk",
            "c0.projection_equivalence",
            "c0.governed_asset_reference",
            "c0.access_policy",
            "c0.rdf_projection_manifest",
            "c0.rdf_projection_candidate_bundle",
            "c0.rdf_serialization_artifact",
            "c0.rdf_validation_receipt",
        } | RUNTIME_CONTRACT_KINDS
    }
    assert previous_hashes == FROZEN_EXISTING_SCHEMA_HASHES
    hashes = write_registered_schemas(tmp_path)
    generated_registry = json.loads(
        (tmp_path / "registry.json").read_text(encoding="utf-8")
    )
    generated_hashes = {
        (item["contract_kind"], item["contract_version"]): item["schema_hash"]
        for item in generated_registry["schemas"]
    }
    assert {
        kind
        for kind, version in generated_hashes
        if kind
        in {
            "c0.publication_crosswalk",
            "c0.projection_equivalence",
            "c0.governed_asset_reference",
            "c0.access_policy",
        }
        and version == "1.0.0"
    } == {
        "c0.publication_crosswalk",
        "c0.projection_equivalence",
        "c0.governed_asset_reference",
        "c0.access_policy",
    }
    for key, digest in previous_hashes.items():
        assert generated_hashes[key] == digest
    assert hashes["c0.publication_crosswalk"] == hashes[
        "c0.publication_crosswalk@1.0.0"
    ]


@pytest.mark.contract
def test_fixture_payloads_reject_unknown_fields_and_wrong_major() -> None:
    payload = json.loads(
        (
            FIXTURES / "valid" / "publication-crosswalk-clinical.json"
        ).read_text(encoding="utf-8")
    )
    unknown = copy.deepcopy(payload)
    unknown["completeness_requirement"] = "must-not-be-owned-here"
    with pytest.raises(ValidationError, match="Extra inputs"):
        parse_contract(unknown)
    wrong_major = copy.deepcopy(payload)
    wrong_major["identity"]["contract_version"] = "2.0.0"
    with pytest.raises(Exception, match="major 2 is unsupported"):
        parse_contract(wrong_major)
