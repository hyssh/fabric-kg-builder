"""Behavior-free C0.RDF contract, fixture, and additivity gates."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts import (
    CanonicalIdentityEnvelope,
    PublicationAuthorityReferences,
    RdfClassDefinition,
    RdfExternalAlignment,
    RdfIriPolicy,
    RdfNamedGraph,
    RdfProjectionAcceptanceBundle,
    RdfProjectionManifest,
    RdfPropertyDefinition,
    RdfRelationshipDefinition,
    RdfSerializationArtifact,
    RdfSerializationObservation,
    RdfSerializedGraphBinding,
    RdfShaclValidationSummary,
    RdfSourceAuthorityTuple,
    RdfValidationReceipt,
    RdfVocabularyInventory,
    canonical_json,
    canonical_sha256,
    parse_contract,
    write_registered_schemas,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "contracts"
ADVERSARIAL = FIXTURES / "adversarial"
SCHEMAS = Path(__file__).parents[2] / "src" / "fabric_kg_builder" / "contracts" / "schemas"
PRE_RDF_BASELINE = json.loads(
    (
        FIXTURES
        / "baselines"
        / "pre-rdf-schema-registry-1.6.0.json"
    ).read_text(encoding="utf-8")
)
SECRET_VALUES = json.loads(
    (ADVERSARIAL / "rdf-secret-values.json").read_text(encoding="utf-8")
)
NONCANONICAL_IRIS = json.loads(
    (ADVERSARIAL / "rdf-noncanonical-iris.json").read_text(encoding="utf-8")
)
BINDING_MUTATIONS = json.loads(
    (ADVERSARIAL / "rdf-artifact-binding-mutations.json").read_text(
        encoding="utf-8"
    )
)


def assert_pre_rdf_baseline(
    *,
    schema_dir: Path,
    current_registry: dict[str, object],
    baseline: dict[str, object],
) -> None:
    expected_files = baseline["files"]
    assert isinstance(expected_files, dict)
    for filename, expected_hash in expected_files.items():
        content = (schema_dir / filename).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected_hash

    expected_entries = baseline["registry_entries"]
    assert isinstance(expected_entries, list)
    current_schema_entries = current_registry["schemas"]
    assert isinstance(current_schema_entries, list)
    for entries in (expected_entries, current_schema_entries):
        keys = [
            (entry["contract_kind"], entry["contract_version"])
            for entry in entries
        ]
        paths = [entry["path"] for entry in entries]
        schema_hashes = [entry["schema_hash"] for entry in entries]
        assert len(keys) == len(set(keys))
        assert len(paths) == len(set(paths))
        assert len(schema_hashes) == len(set(schema_hashes))
    current_entries = {
        (entry["contract_kind"], entry["contract_version"]): entry
        for entry in current_schema_entries
    }
    for entry in expected_entries:
        key = (entry["contract_kind"], entry["contract_version"])
        assert current_entries[key] == entry
H = {
    letter: letter * 64
    for letter in "abcdef0123456789"
}


def identity(kind: str) -> CanonicalIdentityEnvelope:
    return CanonicalIdentityEnvelope(
        contract_kind=kind,
        contract_version="1.0.0",
        project_id="project:rdf-contract",
        asset_id=None,
        asset_version_id=None,
        run_id="run:rdf-contract",
        source_file_id=None,
        source_unit_id=None,
        content_hash=None,
        domain_schema_version="2.0",
        domain_contract_hash=H["b"],
        semantic_contract_hash=H["c"],
        canonical_schema_version="2.0",
        prompt_version=None,
        prompt_hash=None,
        model_version=None,
        model_hash=None,
        extractor_name=None,
        extractor_version=None,
        parent_artifact_ids=("artifact:semantic-serving",),
        parent_record_ids=(),
        immutable_locator=None,
    )


def source_authority() -> RdfSourceAuthorityTuple:
    publication_authority = PublicationAuthorityReferences(
        required_member_manifest_id="required-member-manifest:sealed",
        required_member_manifest_contract_version="1.1.0",
        required_member_manifest_schema_hash=H["a"],
        required_member_manifest_hash=H["b"],
        authoritative_collection_hash=H["c"],
        source_artifact_manifest_id="artifact-manifest:l3-original",
        source_artifact_manifest_hash=H["d"],
    )
    return RdfSourceAuthorityTuple(
        authority="derived",
        semantic_serving_projection_id="semantic-serving-projection:sealed",
        semantic_serving_projection_hash=H["a"],
        l5a_projection_manifest_id="l5a-projection-manifest:sealed",
        l5a_projection_manifest_hash=H["b"],
        publication_crosswalk_id="publication-crosswalk:sealed",
        publication_crosswalk_contract_version="1.1.0",
        publication_crosswalk_schema_hash=H["c"],
        publication_crosswalk_hash=H["d"],
        ontology_projection_equivalence_id="projection-equivalence:ontology",
        ontology_projection_equivalence_hash=H["e"],
        graph_projection_equivalence_id="projection-equivalence:graph",
        graph_projection_equivalence_hash=H["f"],
        search_projection_equivalence_id="projection-equivalence:search",
        search_projection_equivalence_hash=H["0"],
        domain_contract_id="domain-contract:sealed",
        domain_contract_hash=H["b"],
        hierarchy_hash=H["1"],
        identity_policy_hash=H["2"],
        relationship_policy_hash=H["3"],
        k_policy_hash=H["4"],
        instance_canonical_id_set_hash=H["6"],
        instance_canonical_id_count=12,
        provenance_canonical_id_set_hash=H["7"],
        provenance_canonical_id_count=14,
        publication_authority=publication_authority,
    )


def vocabulary() -> RdfVocabularyInventory:
    classes = (
        RdfClassDefinition(
            canonical_class_id="semantic-type:asset",
            class_iri="https://ontology.contoso.test/kg/semantic-type%3Aasset",
            parent_canonical_class_ids=(),
            exact_key_property_ids=("property:asset:id",),
        ),
        RdfClassDefinition(
            canonical_class_id="semantic-type:facility",
            class_iri="https://ontology.contoso.test/kg/semantic-type%3Afacility",
            parent_canonical_class_ids=(),
            exact_key_property_ids=("property:facility:id",),
        ),
    )
    properties = (
        RdfPropertyDefinition(
            canonical_property_id="property:asset:id",
            property_iri="https://ontology.contoso.test/kg/property%3Aasset%3Aid",
            term_kind="datatype_property",
            domain_canonical_class_ids=("semantic-type:asset",),
            range_canonical_ids=(),
            value_type_iris=("https://www.w3.org/2001/XMLSchema#string",),
            endpoint_encoding="single_rdfs_term",
            deterministic_domain_union_node_iri=None,
            deterministic_range_union_node_iri=None,
        ),
        RdfPropertyDefinition(
            canonical_property_id="property:facility:id",
            property_iri="https://ontology.contoso.test/kg/property%3Afacility%3Aid",
            term_kind="datatype_property",
            domain_canonical_class_ids=("semantic-type:facility",),
            range_canonical_ids=(),
            value_type_iris=("https://www.w3.org/2001/XMLSchema#string",),
            endpoint_encoding="single_rdfs_term",
            deterministic_domain_union_node_iri=None,
            deterministic_range_union_node_iri=None,
        ),
    )
    relationships = (
        RdfRelationshipDefinition(
            canonical_relationship_id="relationship:asset:facility",
            relationship_iri=(
                "https://ontology.contoso.test/kg/relationship%3Aasset%3Afacility"
            ),
            source_canonical_class_ids=("semantic-type:asset",),
            target_canonical_class_ids=("semantic-type:facility",),
            endpoint_encoding="single_rdfs_term",
            deterministic_source_union_node_iri=None,
            deterministic_target_union_node_iri=None,
        ),
    )
    values = {
        "owl_profile": "OWL 2 RL compatible derived vocabulary",
        "class_definitions": classes,
        "property_definitions": properties,
        "relationship_definitions": relationships,
        "class_id_set_hash": canonical_sha256(
            sorted(item.canonical_class_id for item in classes)
        ),
        "property_id_set_hash": canonical_sha256(
            sorted(item.canonical_property_id for item in properties)
        ),
        "relationship_id_set_hash": canonical_sha256(
            sorted(item.canonical_relationship_id for item in relationships)
        ),
        "hierarchy_hash": H["1"],
    }
    return RdfVocabularyInventory(
        **values,
        vocabulary_hash=canonical_sha256(values),
    )


def manifest() -> RdfProjectionManifest:
    vocab = vocabulary()
    vocabulary_ids = sorted(
        [item.canonical_class_id for item in vocab.class_definitions]
        + [item.canonical_property_id for item in vocab.property_definitions]
        + [
            item.canonical_relationship_id
            for item in vocab.relationship_definitions
        ]
    )
    vocabulary_id_hash = canonical_sha256(vocabulary_ids)
    graphs = (
        RdfNamedGraph(
            graph_id="graph:common-schema",
            graph_iri="https://ontology.contoso.test/kg/graph/common-schema",
            graph_role="common_schema",
            required=True,
            contains_schema_triples=True,
            contains_instance_or_evidence_triples=False,
            expected_graph_hash=H["1"],
            expected_triple_count=10,
            canonical_id_set_hash=canonical_sha256(()),
            canonical_id_count=0,
        ),
        RdfNamedGraph(
            graph_id="graph:domain-schema",
            graph_iri="https://ontology.contoso.test/kg/graph/domain-schema",
            graph_role="domain_schema",
            required=True,
            contains_schema_triples=True,
            contains_instance_or_evidence_triples=False,
            expected_graph_hash=H["2"],
            expected_triple_count=10,
            canonical_id_set_hash=vocabulary_id_hash,
            canonical_id_count=len(vocabulary_ids),
        ),
        RdfNamedGraph(
            graph_id="graph:provenance",
            graph_iri="https://ontology.contoso.test/kg/graph/provenance",
            graph_role="provenance_authority",
            required=True,
            contains_schema_triples=False,
            contains_instance_or_evidence_triples=True,
            expected_graph_hash=H["3"],
            expected_triple_count=12,
            canonical_id_set_hash=H["7"],
            canonical_id_count=14,
            access_policy_id="access-policy:rdf-protected",
            access_policy_hash=H["5"],
        ),
        RdfNamedGraph(
            graph_id="graph:shapes",
            graph_iri="https://ontology.contoso.test/kg/graph/shapes",
            graph_role="shacl_shapes",
            required=True,
            contains_schema_triples=True,
            contains_instance_or_evidence_triples=False,
            expected_graph_hash=H["4"],
            expected_triple_count=10,
            canonical_id_set_hash=vocabulary_id_hash,
            canonical_id_count=len(vocabulary_ids),
        ),
    )
    authority = source_authority()
    values = {
        "identity": identity("c0.rdf_projection_manifest"),
        "rdf_projection_manifest_id": "rdf-projection-manifest:generic",
        "source_authority": authority,
        "authority_reference_set_hash": authority.reference_set_hash(),
        "iri_policy": RdfIriPolicy(
            namespace_governance_id="namespace-governance:rdf",
            namespace_governance_hash=H["0"],
            ontology_base_iri="https://ontology.contoso.test/kg/",
            instance_base_iri="https://data.contoso.test/kg/",
            ontology_iri="https://ontology.contoso.test/kg/ontology",
            version_iri="https://ontology.contoso.test/kg/version/1.0.0",
            ontology_semantic_version="1.0.0",
        ),
        "named_graphs": graphs,
        "vocabulary": vocab,
        "external_alignments": (
            RdfExternalAlignment(
                target_iri="https://standards.contoso.test/vocabulary/asset",
                relation_kind="rdfs_see_also",
                source_artifact_reference_id="governed-asset:alignment",
                source_artifact_version="release-7",
                source_artifact_hash=H["6"],
                source_license_id="license:approved",
                approval_reference_id="approval:alignment",
                approval_hash=H["7"],
            ),
        ),
        "required_serialization_formats": (
            "canonical_n_quads",
            "rdf_xml",
            "turtle",
        ),
        "full_source_quotes_forbidden": True,
        "transient_or_signed_urls_forbidden": True,
        "evidence_reference_policy": "ids_hashes_and_prov_links_only",
        "required_member_authority": "c0.required_member_manifest",
    }
    return RdfProjectionManifest(
        **values,
        projection_manifest_hash=canonical_sha256(values),
    )


def artifact(
    rdf_format: str,
    *,
    dataset_hash: str = H["8"],
    artifact_id: str | None = None,
    exposure: str = "protected_dataset",
) -> RdfSerializationArtifact:
    profile = {
        "turtle": ("text/turtle", "RDF 1.1 Turtle"),
        "rdf_xml": ("application/rdf+xml", "RDF 1.1 XML Syntax"),
        "json_ld": ("application/ld+json", "JSON-LD 1.1"),
        "canonical_n_quads": ("application/n-quads", "RDF 1.1 N-Quads"),
    }[rdf_format]
    projection = manifest()
    graph_bindings = tuple(
        RdfSerializedGraphBinding(
            graph_id=graph.graph_id,
            graph_iri=graph.graph_iri,
            graph_role=graph.graph_role,
            required=graph.required,
            triple_count=graph.expected_triple_count,
            graph_hash=graph.expected_graph_hash,
            canonical_id_set_hash=graph.canonical_id_set_hash,
            canonical_id_count=graph.canonical_id_count,
            access_policy_id=graph.access_policy_id,
            access_policy_hash=graph.access_policy_hash,
        )
        for graph in projection.named_graphs
        if (
            exposure == "public_schema"
            and graph.graph_role in {"common_schema", "domain_schema", "shacl_shapes"}
        )
        or (
            exposure == "protected_dataset"
            and graph.graph_role in {"instances", "provenance_authority"}
        )
    )
    named_graph_ids = tuple(item.graph_id for item in graph_bindings)
    values = {
        "identity": identity("c0.rdf_serialization_artifact"),
        "rdf_serialization_artifact_id": (
            artifact_id or f"rdf-artifact:{rdf_format}:{exposure}"
        ),
        "rdf_projection_manifest_id": projection.rdf_projection_manifest_id,
        "rdf_projection_manifest_hash": projection.projection_manifest_hash,
        "authority_reference_set_hash": projection.authority_reference_set_hash,
        "serialization_format": rdf_format,
        "media_type": profile[0],
        "w3c_syntax_version": profile[1],
        "exposure": exposure,
        "content_hash": H["9"],
        "byte_count": 1024,
        "triple_count": sum(item.triple_count for item in graph_bindings),
        "graph_count": len(graph_bindings),
        "named_graph_ids": named_graph_ids,
        "graph_bindings": graph_bindings,
        "graph_inventory_hash": canonical_sha256(graph_bindings),
        "canonical_id_binding_hash": canonical_sha256(
            tuple(
                {
                    "graph_id": item.graph_id,
                    "graph_role": item.graph_role,
                    "canonical_id_set_hash": item.canonical_id_set_hash,
                    "canonical_id_count": item.canonical_id_count,
                }
                for item in graph_bindings
            )
        ),
        "canonical_dataset_hash_algorithm": "RDFC-1.0",
        "canonical_dataset_hash": dataset_hash,
        "blank_node_policy": "none_after_deterministic_skolemization",
        "access_policy_id": (
            "access-policy:rdf-protected"
            if exposure == "protected_dataset"
            else None
        ),
        "access_policy_hash": H["5"] if exposure == "protected_dataset" else None,
    }
    return RdfSerializationArtifact(
        **values,
        serialization_artifact_hash=canonical_sha256(values),
    )


def validation_receipt(*, drift: bool = False) -> RdfValidationReceipt:
    formats = ("turtle", "rdf_xml", "canonical_n_quads")
    artifacts = {
        (rdf_format, exposure): artifact(rdf_format, exposure=exposure)
        for rdf_format in formats
        for exposure in ("public_schema", "protected_dataset")
    }
    projection = manifest()
    observations = tuple(
        RdfSerializationObservation(
            rdf_serialization_artifact_id=artifacts[
                (rdf_format, exposure)
            ].rdf_serialization_artifact_id,
            rdf_serialization_artifact_hash=artifacts[
                (rdf_format, exposure)
            ].serialization_artifact_hash,
            serialization_format=rdf_format,
            exposure=exposure,
            media_type=artifacts[(rdf_format, exposure)].media_type,
            content_hash=artifacts[(rdf_format, exposure)].content_hash,
            canonical_dataset_hash=H["7"] if drift and rdf_format == "turtle" else H["8"],
            named_graph_ids=artifacts[(rdf_format, exposure)].named_graph_ids,
            graph_inventory_hash=artifacts[
                (rdf_format, exposure)
            ].graph_inventory_hash,
            canonical_id_binding_hash=artifacts[
                (rdf_format, exposure)
            ].canonical_id_binding_hash,
            triple_count=artifacts[(rdf_format, exposure)].triple_count,
            missing_triple_count=0,
            extra_triple_count=0,
            authority_reference_set_hash=projection.authority_reference_set_hash,
            base_iri_matches=True,
            label_identity_detected=False,
            unstable_blank_node_detected=False,
        )
        for rdf_format in formats
        for exposure in ("public_schema", "protected_dataset")
    )
    observations = tuple(
        sorted(observations, key=lambda item: item.rdf_serialization_artifact_id)
    )
    values = {
        "identity": identity("c0.rdf_validation_receipt"),
        "rdf_validation_receipt_id": "rdf-validation-receipt:generic",
        "rdf_projection_manifest_id": projection.rdf_projection_manifest_id,
        "rdf_projection_manifest_hash": projection.projection_manifest_hash,
        "source_authority_hash": canonical_sha256(source_authority()),
        "authority_reference_set_hash": projection.authority_reference_set_hash,
        "canonical_id_partition_binding_hash": canonical_sha256(
            tuple(
                {
                    "rdf_serialization_artifact_id": item.rdf_serialization_artifact_id,
                    "serialization_format": item.serialization_format,
                    "exposure": item.exposure,
                    "canonical_id_binding_hash": item.canonical_id_binding_hash,
                }
                for item in observations
            )
        ),
        "canonical_n_quads_artifact_ids": tuple(
            item.rdf_serialization_artifact_id
            for item in observations
            if item.serialization_format == "canonical_n_quads"
        ),
        "canonical_dataset_hash_algorithm": "RDFC-1.0",
        "canonical_dataset_hash": H["8"],
        "required_serialization_formats": projection.required_serialization_formats,
        "observations": observations,
        "shacl_validation": RdfShaclValidationSummary(
            shapes_hash=H["4"],
            conforms=True,
            violation_count=0,
            warning_count=0,
            info_count=0,
            validation_report_hash=H["4"],
            validator_id="validator:approved",
            validator_version="1.2.0",
        ),
        "exact_round_trip_equivalent": not drift,
    }
    return RdfValidationReceipt(
        **values,
        validation_receipt_hash=canonical_sha256(values),
    )


def acceptance_bundle() -> RdfProjectionAcceptanceBundle:
    projection = manifest()
    artifacts = tuple(
        sorted(
            (
                artifact(rdf_format, exposure=exposure)
                for rdf_format in projection.required_serialization_formats
                for exposure in ("public_schema", "protected_dataset")
            ),
            key=lambda item: item.rdf_serialization_artifact_id,
        )
    )
    receipt = validation_receipt()
    values = {
        "identity": identity("c0.rdf_projection_acceptance_bundle"),
        "rdf_projection_acceptance_bundle_id": "rdf-acceptance-bundle:generic",
        "authority_reference_set_hash": projection.authority_reference_set_hash,
        "canonical_id_partition_binding_hash": (
            receipt.canonical_id_partition_binding_hash
        ),
        "manifest": projection,
        "serialization_artifacts": artifacts,
        "validation_receipt": receipt,
        "acceptance_status": "accepted",
    }
    return RdfProjectionAcceptanceBundle(
        **values,
        acceptance_bundle_hash=canonical_sha256(values),
    )


def reseal_manifest_payload(payload: dict[str, object]) -> None:
    payload["required_serialization_formats"] = sorted(
        set(payload["required_serialization_formats"])
    )
    vocabulary_payload = payload["vocabulary"]
    assert isinstance(vocabulary_payload, dict)
    vocabulary_payload["class_id_set_hash"] = canonical_sha256(
        sorted(
            item["canonical_class_id"]
            for item in vocabulary_payload["class_definitions"]
        )
    )
    vocabulary_payload["property_id_set_hash"] = canonical_sha256(
        sorted(
            item["canonical_property_id"]
            for item in vocabulary_payload["property_definitions"]
        )
    )
    vocabulary_payload["relationship_id_set_hash"] = canonical_sha256(
        sorted(
            item["canonical_relationship_id"]
            for item in vocabulary_payload["relationship_definitions"]
        )
    )
    vocabulary_payload["vocabulary_hash"] = canonical_sha256(
        {
            key: value
            for key, value in vocabulary_payload.items()
            if key != "vocabulary_hash"
        }
    )
    payload["projection_manifest_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "projection_manifest_hash"
        }
    )


@pytest.mark.contract
def test_rdf_contracts_are_strict_derived_views() -> None:
    projection = manifest()
    assert projection.source_authority.authority == "derived"
    assert projection.required_member_authority == "c0.required_member_manifest"
    assert projection.full_source_quotes_forbidden
    assert projection.transient_or_signed_urls_forbidden
    assert projection.external_alignments[0].import_policy == (
        "metadata_only_no_import_or_fetch"
    )
    assert parse_contract(canonical_json(projection)) == projection
    for rdf_format in ("turtle", "rdf_xml", "json_ld", "canonical_n_quads"):
        assert parse_contract(canonical_json(artifact(rdf_format))) == artifact(rdf_format)
    receipt = validation_receipt()
    assert parse_contract(canonical_json(receipt)) == receipt
    receipt.validate_against_manifest_and_artifacts(
        projection,
        tuple(
            artifact(rdf_format, exposure=exposure)
            for rdf_format in projection.required_serialization_formats
            for exposure in ("public_schema", "protected_dataset")
        ),
    )
    accepted = acceptance_bundle()
    assert parse_contract(canonical_json(accepted)) == accepted


@pytest.mark.contract
def test_acceptance_bundle_rejects_coordinated_forged_children() -> None:
    accepted = acceptance_bundle()

    payload = accepted.model_dump(mode="json")
    payload["validation_receipt"]["observations"][0][
        "rdf_serialization_artifact_hash"
    ] = BINDING_MUTATIONS["artifact_hash"]
    payload["validation_receipt"]["validation_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload["validation_receipt"].items()
            if key != "validation_receipt_hash"
        }
    )
    payload["acceptance_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "acceptance_bundle_hash"
        }
    )
    with pytest.raises(ValidationError, match="artifact hash mismatch"):
        RdfProjectionAcceptanceBundle.model_validate(payload)

    payload = accepted.model_dump(mode="json")
    forged_artifact = payload["serialization_artifacts"][0]
    forged_artifact["rdf_projection_manifest_hash"] = H["0"]
    forged_artifact["serialization_artifact_hash"] = canonical_sha256(
        {
            key: value
            for key, value in forged_artifact.items()
            if key != "serialization_artifact_hash"
        }
    )
    for observation in payload["validation_receipt"]["observations"]:
        if (
            observation["rdf_serialization_artifact_id"]
            == forged_artifact["rdf_serialization_artifact_id"]
        ):
            observation["rdf_serialization_artifact_hash"] = forged_artifact[
                "serialization_artifact_hash"
            ]
    payload["validation_receipt"]["validation_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload["validation_receipt"].items()
            if key != "validation_receipt_hash"
        }
    )
    payload["acceptance_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "acceptance_bundle_hash"
        }
    )
    with pytest.raises(ValidationError, match="manifest hash mismatch"):
        RdfProjectionAcceptanceBundle.model_validate(payload)

    payload = accepted.model_dump(mode="json")
    payload["manifest"]["required_serialization_formats"].append("json_ld")
    reseal_manifest_payload(payload["manifest"])
    payload["acceptance_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "acceptance_bundle_hash"
        }
    )
    with pytest.raises(ValidationError, match="manifest hash mismatch"):
        RdfProjectionAcceptanceBundle.model_validate(payload)


@pytest.mark.contract
def test_multi_endpoint_rdfs_intersection_encoding_fails_closed() -> None:
    payload = vocabulary().property_definitions[0].model_dump(mode="json")
    payload["domain_canonical_class_ids"] = [
        "semantic-type:asset",
        "semantic-type:facility",
    ]
    with pytest.raises(ValidationError, match="multiple endpoints"):
        RdfPropertyDefinition.model_validate(payload)


@pytest.mark.contract
def test_endpoint_union_nodes_are_side_specific_and_deterministic() -> None:
    payload = manifest().model_dump(mode="json")
    relationship = payload["vocabulary"]["relationship_definitions"][0]
    relationship["source_canonical_class_ids"] = [
        "semantic-type:asset",
        "semantic-type:facility",
    ]
    relationship["endpoint_encoding"] = "named_owl_union"
    endpoint_hash = canonical_sha256(relationship["source_canonical_class_ids"])
    source_seed = (
        "endpoint-union:relationship:asset:facility:source:"
        f"{endpoint_hash}"
    )
    relationship["deterministic_source_union_node_iri"] = (
        "https://ontology.contoso.test/kg/" + quote(source_seed, safe="")
    )
    reseal_manifest_payload(payload)
    projection = RdfProjectionManifest.model_validate(payload)
    sealed = projection.vocabulary.relationship_definitions[0]
    assert sealed.deterministic_source_union_node_iri is not None
    assert sealed.deterministic_target_union_node_iri is None

    missing = copy.deepcopy(payload)
    missing["vocabulary"]["relationship_definitions"][0][
        "deterministic_source_union_node_iri"
    ] = None
    with pytest.raises(ValidationError, match="required iff"):
        RdfProjectionManifest.model_validate(missing)

    swapped = copy.deepcopy(payload)
    target_seed = (
        "endpoint-union:relationship:asset:facility:target:"
        f"{endpoint_hash}"
    )
    swapped["vocabulary"]["relationship_definitions"][0][
        "deterministic_source_union_node_iri"
    ] = "https://ontology.contoso.test/kg/" + quote(target_seed, safe="")
    reseal_manifest_payload(swapped)
    with pytest.raises(ValidationError, match="deterministic governed mapping"):
        RdfProjectionManifest.model_validate(swapped)

    extra = copy.deepcopy(payload)
    extra["vocabulary"]["relationship_definitions"][0][
        "deterministic_target_union_node_iri"
    ] = "https://ontology.contoso.test/kg/" + quote(target_seed, safe="")
    with pytest.raises(ValidationError, match="required iff"):
        RdfProjectionManifest.model_validate(extra)

    reused = copy.deepcopy(payload)
    reused_relationship = reused["vocabulary"]["relationship_definitions"][0]
    reused_relationship["target_canonical_class_ids"] = [
        "semantic-type:asset",
        "semantic-type:facility",
    ]
    reused_relationship["deterministic_target_union_node_iri"] = (
        reused_relationship["deterministic_source_union_node_iri"]
    )
    with pytest.raises(ValidationError, match="must be distinct"):
        RdfProjectionManifest.model_validate(reused)


@pytest.mark.contract
def test_property_inventory_cannot_encode_a_class_as_a_property() -> None:
    payload = vocabulary().property_definitions[0].model_dump(mode="json")
    payload["term_kind"] = "class"
    with pytest.raises(ValidationError, match="term_kind"):
        RdfPropertyDefinition.model_validate(payload)


@pytest.mark.contract
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"ontology_base_iri": "http://ontology.contoso.test/kg/"}, "HTTPS IRI"),
    ],
)
def test_iri_policy_rejects_non_https_namespaces(
    mutation: dict[str, str],
    message: str,
) -> None:
    payload = manifest().iri_policy.model_dump(mode="json")
    payload.update(mutation)
    with pytest.raises(ValidationError, match=message):
        RdfIriPolicy.model_validate(payload)


@pytest.mark.contract
def test_iri_policy_requires_explicit_namespace_governance() -> None:
    payload = manifest().iri_policy.model_dump(mode="json")
    del payload["namespace_governance_id"]
    with pytest.raises(ValidationError, match="namespace_governance_id"):
        RdfIriPolicy.model_validate(payload)


@pytest.mark.contract
def test_serialization_profile_and_access_boundaries_fail_closed() -> None:
    payload = artifact("turtle").model_dump(mode="json")
    payload["media_type"] = "application/rdf+xml"
    with pytest.raises(ValidationError, match="media type"):
        RdfSerializationArtifact.model_validate(payload)

    payload = artifact("turtle").model_dump(mode="json")
    payload.update(
        exposure="public_schema",
        access_policy_id="access-policy:must-not-leak",
        access_policy_hash=H["5"],
    )
    with pytest.raises(ValidationError, match="ACL principal"):
        RdfSerializationArtifact.model_validate(payload)


@pytest.mark.contract
def test_public_artifact_cannot_relabel_or_include_protected_graphs() -> None:
    public = artifact("turtle", exposure="public_schema")
    public.validate_against_manifest(manifest())
    payload = public.model_dump(mode="json")
    protected = artifact("turtle").graph_bindings[0]
    payload["graph_bindings"].append(protected.model_dump(mode="json"))
    payload["named_graph_ids"].append(protected.graph_id)
    payload["graph_bindings"].sort(key=lambda item: item["graph_id"])
    payload["named_graph_ids"].sort()
    payload["graph_count"] += 1
    payload["triple_count"] += protected.triple_count
    payload["graph_inventory_hash"] = canonical_sha256(payload["graph_bindings"])
    payload["canonical_id_binding_hash"] = canonical_sha256(
        tuple(
            {
                "graph_id": item["graph_id"],
                "graph_role": item["graph_role"],
                "canonical_id_set_hash": item["canonical_id_set_hash"],
                "canonical_id_count": item["canonical_id_count"],
            }
            for item in payload["graph_bindings"]
        )
    )
    payload["serialization_artifact_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "serialization_artifact_hash"
        }
    )
    with pytest.raises(ValidationError, match="public.*graph roles"):
        RdfSerializationArtifact.model_validate(payload)

    payload = artifact("turtle").model_dump(mode="json")
    payload["graph_bindings"][0]["graph_role"] = "domain_schema"
    payload["graph_bindings"][0]["access_policy_id"] = None
    payload["graph_bindings"][0]["access_policy_hash"] = None
    payload["graph_inventory_hash"] = canonical_sha256(payload["graph_bindings"])
    payload["canonical_id_binding_hash"] = canonical_sha256(
        tuple(
            {
                "graph_id": item["graph_id"],
                "graph_role": item["graph_role"],
                "canonical_id_set_hash": item["canonical_id_set_hash"],
                "canonical_id_count": item["canonical_id_count"],
            }
            for item in payload["graph_bindings"]
        )
    )
    payload["serialization_artifact_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "serialization_artifact_hash"
        }
    )
    with pytest.raises(ValidationError, match="require provenance"):
        RdfSerializationArtifact.model_validate(payload)


@pytest.mark.contract
def test_receipt_binds_exact_manifest_and_artifact_seals() -> None:
    projection = manifest()
    artifacts = tuple(
        artifact(rdf_format, exposure=exposure)
        for rdf_format in projection.required_serialization_formats
        for exposure in ("public_schema", "protected_dataset")
    )
    receipt = validation_receipt()
    receipt.validate_against_manifest_and_artifacts(projection, artifacts)

    payload = receipt.model_dump(mode="json")
    payload["observations"][0]["rdf_serialization_artifact_hash"] = (
        BINDING_MUTATIONS["artifact_hash"]
    )
    payload["validation_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "validation_receipt_hash"
        }
    )
    resealed = RdfValidationReceipt.model_validate(payload)
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        resealed.validate_against_manifest_and_artifacts(projection, artifacts)

    with pytest.raises(ValueError, match="exact artifact set"):
        receipt.validate_against_manifest_and_artifacts(projection, artifacts[:-1])


@pytest.mark.contract
def test_acceptance_requires_complete_disjoint_partitions_per_format() -> None:
    projection = manifest()
    receipt = validation_receipt()
    complete = tuple(
        artifact(rdf_format, exposure=exposure)
        for rdf_format in projection.required_serialization_formats
        for exposure in ("public_schema", "protected_dataset")
    )
    receipt.validate_against_manifest_and_artifacts(projection, complete)

    public_only = tuple(
        item for item in complete if item.exposure == "public_schema"
    )
    with pytest.raises(ValueError, match="exact artifact set"):
        receipt.validate_against_manifest_and_artifacts(projection, public_only)
    public_only_bundle = acceptance_bundle().model_dump(mode="json")
    public_only_bundle["serialization_artifacts"] = [
        item
        for item in public_only_bundle["serialization_artifacts"]
        if item["exposure"] == "public_schema"
    ]
    public_only_bundle["acceptance_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in public_only_bundle.items()
            if key != "acceptance_bundle_hash"
        }
    )
    with pytest.raises(ValidationError, match="exact artifact set"):
        RdfProjectionAcceptanceBundle.model_validate(public_only_bundle)

    missing_protected_format = tuple(
        item
        for item in complete
        if not (
            item.serialization_format == "turtle"
            and item.exposure == "protected_dataset"
        )
    )
    with pytest.raises(ValueError, match="exact artifact set"):
        receipt.validate_against_manifest_and_artifacts(
            projection,
            missing_protected_format,
        )

    duplicate_graph = artifact("turtle", exposure="protected_dataset").model_dump(
        mode="json"
    )
    public_graph = artifact("turtle", exposure="public_schema").graph_bindings[0]
    duplicate_graph["graph_bindings"].append(public_graph.model_dump(mode="json"))
    duplicate_graph["graph_bindings"].sort(key=lambda item: item["graph_id"])
    duplicate_graph["named_graph_ids"] = [
        item["graph_id"] for item in duplicate_graph["graph_bindings"]
    ]
    duplicate_graph["graph_count"] = len(duplicate_graph["graph_bindings"])
    duplicate_graph["triple_count"] = sum(
        item["triple_count"] for item in duplicate_graph["graph_bindings"]
    )
    duplicate_graph["graph_inventory_hash"] = canonical_sha256(
        duplicate_graph["graph_bindings"]
    )
    duplicate_graph["canonical_id_binding_hash"] = canonical_sha256(
        tuple(
            {
                "graph_id": item["graph_id"],
                "graph_role": item["graph_role"],
                "canonical_id_set_hash": item["canonical_id_set_hash"],
                "canonical_id_count": item["canonical_id_count"],
            }
            for item in duplicate_graph["graph_bindings"]
        )
    )
    duplicate_graph["serialization_artifact_hash"] = canonical_sha256(
        {
            key: value
            for key, value in duplicate_graph.items()
            if key != "serialization_artifact_hash"
        }
    )
    with pytest.raises(ValidationError, match="provenance and optional instances"):
        RdfSerializationArtifact.model_validate(duplicate_graph)


@pytest.mark.contract
def test_acceptance_bundle_binds_manifest_shacl_shapes_graph_hash() -> None:
    manifest_payload = manifest().model_dump(mode="json")
    shapes = next(
        graph
        for graph in manifest_payload["named_graphs"]
        if graph["graph_role"] == "shacl_shapes"
    )
    shapes["expected_graph_hash"] = H["9"]
    reseal_manifest_payload(manifest_payload)
    projection = RdfProjectionManifest.model_validate(manifest_payload)

    artifacts = []
    for rdf_format in projection.required_serialization_formats:
        for exposure in ("public_schema", "protected_dataset"):
            artifact_payload = artifact(
                rdf_format,
                exposure=exposure,
            ).model_dump(mode="json")
            artifact_payload["rdf_projection_manifest_hash"] = (
                projection.projection_manifest_hash
            )
            if exposure == "public_schema":
                artifact_shapes = next(
                    graph
                    for graph in artifact_payload["graph_bindings"]
                    if graph["graph_role"] == "shacl_shapes"
                )
                artifact_shapes["graph_hash"] = H["9"]
                artifact_payload["graph_inventory_hash"] = canonical_sha256(
                    artifact_payload["graph_bindings"]
                )
            artifact_payload["serialization_artifact_hash"] = canonical_sha256(
                {
                    key: value
                    for key, value in artifact_payload.items()
                    if key != "serialization_artifact_hash"
                }
            )
            artifacts.append(
                RdfSerializationArtifact.model_validate(artifact_payload)
            )

    receipt_payload = validation_receipt().model_dump(mode="json")
    receipt_payload["rdf_projection_manifest_hash"] = projection.projection_manifest_hash
    artifact_by_id = {
        item.rdf_serialization_artifact_id: item for item in artifacts
    }
    for observation in receipt_payload["observations"]:
        sealed = artifact_by_id[observation["rdf_serialization_artifact_id"]]
        observation["rdf_serialization_artifact_hash"] = (
            sealed.serialization_artifact_hash
        )
        observation["graph_inventory_hash"] = sealed.graph_inventory_hash
    receipt_payload["validation_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt_payload.items()
            if key != "validation_receipt_hash"
        }
    )
    receipt = RdfValidationReceipt.model_validate(receipt_payload)
    with pytest.raises(ValueError, match="SHACL shapes hash"):
        receipt.validate_against_manifest_and_artifacts(projection, artifacts)

    bundle_values = {
        "identity": identity("c0.rdf_projection_acceptance_bundle"),
        "rdf_projection_acceptance_bundle_id": "rdf-acceptance-bundle:forged-shapes",
        "authority_reference_set_hash": projection.authority_reference_set_hash,
        "canonical_id_partition_binding_hash": (
            receipt.canonical_id_partition_binding_hash
        ),
        "manifest": projection,
        "serialization_artifacts": tuple(artifacts),
        "validation_receipt": receipt,
        "acceptance_status": "accepted",
    }
    with pytest.raises(ValidationError, match="SHACL shapes hash"):
        RdfProjectionAcceptanceBundle(
            **bundle_values,
            acceptance_bundle_hash=canonical_sha256(bundle_values),
        )


@pytest.mark.contract
def test_authority_reference_set_cannot_be_self_agreed_downstream() -> None:
    manifest_payload = manifest().model_dump(mode="json")
    manifest_payload["source_authority"]["domain_contract_id"] = (
        "domain-contract:changed"
    )
    with pytest.raises(ValidationError, match="authority_reference_set_hash"):
        RdfProjectionManifest.model_validate(manifest_payload)

    manifest_payload = manifest().model_dump(mode="json")
    manifest_payload["source_authority"]["extra_reference"] = "forbidden"
    with pytest.raises(ValidationError, match="extra"):
        RdfProjectionManifest.model_validate(manifest_payload)

    payload = acceptance_bundle().model_dump(mode="json")
    for artifact_payload in payload["serialization_artifacts"]:
        artifact_payload["authority_reference_set_hash"] = H["9"]
        artifact_payload["serialization_artifact_hash"] = canonical_sha256(
            {
                key: value
                for key, value in artifact_payload.items()
                if key != "serialization_artifact_hash"
            }
        )
    artifact_hashes = {
        item["rdf_serialization_artifact_id"]: item["serialization_artifact_hash"]
        for item in payload["serialization_artifacts"]
    }
    payload["validation_receipt"]["authority_reference_set_hash"] = H["9"]
    for observation in payload["validation_receipt"]["observations"]:
        observation["authority_reference_set_hash"] = H["9"]
        observation["rdf_serialization_artifact_hash"] = artifact_hashes[
            observation["rdf_serialization_artifact_id"]
        ]
    payload["validation_receipt"]["validation_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload["validation_receipt"].items()
            if key != "validation_receipt_hash"
        }
    )
    payload["acceptance_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "acceptance_bundle_hash"
        }
    )
    with pytest.raises(
        ValidationError,
        match="authority reference set hash mismatch",
    ):
        RdfProjectionAcceptanceBundle.model_validate(payload)


@pytest.mark.contract
def test_canonical_id_graph_commitments_are_manifest_authority() -> None:
    projection = manifest()
    public = artifact("turtle", exposure="public_schema")
    protected = artifact("turtle", exposure="protected_dataset")
    assert public.canonical_id_binding_hash == projection.canonical_id_binding_hash(
        "public_schema"
    )
    assert protected.canonical_id_binding_hash == projection.canonical_id_binding_hash(
        "protected_dataset"
    )
    assert public.canonical_id_binding_hash != protected.canonical_id_binding_hash

    manifest_payload = projection.model_dump(mode="json")
    manifest_payload["named_graphs"][0]["canonical_id_set_hash"] = H["f"]
    manifest_payload["projection_manifest_hash"] = canonical_sha256(
        {
            key: value
            for key, value in manifest_payload.items()
            if key != "projection_manifest_hash"
        }
    )
    with pytest.raises(ValidationError, match="canonical ID commitment"):
        RdfProjectionManifest.model_validate(manifest_payload)

    artifact_payload = public.model_dump(mode="json")
    artifact_payload["graph_bindings"][0]["canonical_id_set_hash"] = H["f"]
    artifact_payload["graph_bindings"][1]["canonical_id_set_hash"] = (
        public.graph_bindings[0].canonical_id_set_hash
    )
    artifact_payload["graph_inventory_hash"] = canonical_sha256(
        artifact_payload["graph_bindings"]
    )
    artifact_payload["canonical_id_binding_hash"] = canonical_sha256(
        tuple(
            {
                "graph_id": item["graph_id"],
                "graph_role": item["graph_role"],
                "canonical_id_set_hash": item["canonical_id_set_hash"],
                "canonical_id_count": item["canonical_id_count"],
            }
            for item in artifact_payload["graph_bindings"]
        )
    )
    artifact_payload["serialization_artifact_hash"] = canonical_sha256(
        {
            key: value
            for key, value in artifact_payload.items()
            if key != "serialization_artifact_hash"
        }
    )
    swapped = RdfSerializationArtifact.model_validate(artifact_payload)
    with pytest.raises(ValueError, match="binding differs from manifest"):
        swapped.validate_against_manifest(projection)

    payload = acceptance_bundle().model_dump(mode="json")
    artifact_hashes: dict[str, str] = {}
    binding_hashes: dict[str, str] = {}
    for item in payload["serialization_artifacts"]:
        item["graph_bindings"][0]["canonical_id_set_hash"] = H["f"]
        item["graph_inventory_hash"] = canonical_sha256(item["graph_bindings"])
        item["canonical_id_binding_hash"] = canonical_sha256(
            tuple(
                {
                    "graph_id": graph["graph_id"],
                    "graph_role": graph["graph_role"],
                    "canonical_id_set_hash": graph["canonical_id_set_hash"],
                    "canonical_id_count": graph["canonical_id_count"],
                }
                for graph in item["graph_bindings"]
            )
        )
        binding_hashes[item["rdf_serialization_artifact_id"]] = item[
            "canonical_id_binding_hash"
        ]
        item["serialization_artifact_hash"] = canonical_sha256(
            {
                key: value
                for key, value in item.items()
                if key != "serialization_artifact_hash"
            }
        )
        artifact_hashes[item["rdf_serialization_artifact_id"]] = item[
            "serialization_artifact_hash"
        ]
    for observation in payload["validation_receipt"]["observations"]:
        observation["canonical_id_binding_hash"] = binding_hashes[
            observation["rdf_serialization_artifact_id"]
        ]
        observation["rdf_serialization_artifact_hash"] = artifact_hashes[
            observation["rdf_serialization_artifact_id"]
        ]
    partition_hash = canonical_sha256(
        tuple(
            {
                "rdf_serialization_artifact_id": item[
                    "rdf_serialization_artifact_id"
                ],
                "serialization_format": item["serialization_format"],
                "exposure": item["exposure"],
                "canonical_id_binding_hash": item["canonical_id_binding_hash"],
            }
            for item in payload["validation_receipt"]["observations"]
        )
    )
    payload["canonical_id_partition_binding_hash"] = partition_hash
    payload["validation_receipt"][
        "canonical_id_partition_binding_hash"
    ] = partition_hash
    payload["validation_receipt"]["validation_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload["validation_receipt"].items()
            if key != "validation_receipt_hash"
        }
    )
    payload["acceptance_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "acceptance_bundle_hash"
        }
    )
    with pytest.raises(
        ValidationError,
        match="canonical ID binding differs from manifest",
    ):
        RdfProjectionAcceptanceBundle.model_validate(payload)


@pytest.mark.contract
def test_receipt_formats_equal_manifest_including_required_json_ld() -> None:
    payload = manifest().model_dump(mode="json")
    payload["required_serialization_formats"].append("json_ld")
    reseal_manifest_payload(payload)
    projection = RdfProjectionManifest.model_validate(payload)
    receipt_payload = validation_receipt().model_dump(mode="json")
    receipt_payload["rdf_projection_manifest_hash"] = projection.projection_manifest_hash
    receipt_payload["validation_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt_payload.items()
            if key != "validation_receipt_hash"
        }
    )
    receipt = RdfValidationReceipt.model_validate(receipt_payload)
    artifacts = tuple(
        artifact(rdf_format, exposure=exposure)
        for rdf_format in receipt.required_serialization_formats
        for exposure in ("public_schema", "protected_dataset")
    )
    with pytest.raises(ValueError, match="required formats differ"):
        receipt.validate_against_manifest_and_artifacts(projection, artifacts)

    receipt_payload = receipt.model_dump(mode="json")
    receipt_payload["required_serialization_formats"].append("json_ld")
    with pytest.raises(ValidationError, match="exactly equal"):
        RdfValidationReceipt.model_validate(receipt_payload)


@pytest.mark.contract
def test_manifest_rejects_duplicate_graph_roles_and_stale_hierarchy() -> None:
    payload = manifest().model_dump(mode="json")
    payload["named_graphs"][1]["graph_role"] = "common_schema"
    with pytest.raises(ValidationError, match="graph roles"):
        RdfProjectionManifest.model_validate(payload)

    payload = manifest().model_dump(mode="json")
    payload["vocabulary"]["hierarchy_hash"] = H["9"]
    payload["vocabulary"]["vocabulary_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload["vocabulary"].items()
            if key != "vocabulary_hash"
        }
    )
    with pytest.raises(ValidationError, match="hierarchy differs"):
        RdfProjectionManifest.model_validate(payload)

    payload = manifest().model_dump(mode="json")
    payload["named_graphs"][0]["required"] = False
    with pytest.raises(ValidationError, match="required=true"):
        RdfProjectionManifest.model_validate(payload)


@pytest.mark.contract
@pytest.mark.parametrize("bad_iri", NONCANONICAL_IRIS)
def test_vocabulary_iris_require_exact_governed_canonical_mapping(
    bad_iri: str,
) -> None:
    payload = manifest().model_dump(mode="json")
    payload["vocabulary"]["class_definitions"][0]["class_iri"] = bad_iri
    reseal_manifest_payload(payload)
    with pytest.raises(ValidationError, match="canonical-ID mapping"):
        RdfProjectionManifest.model_validate(payload)

    payload = manifest().model_dump(mode="json")
    payload["vocabulary"]["class_definitions"][1]["class_iri"] = payload[
        "vocabulary"
    ]["class_definitions"][0]["class_iri"]
    reseal_manifest_payload(payload)
    with pytest.raises(ValidationError, match="globally injective"):
        RdfProjectionManifest.model_validate(payload)


@pytest.mark.contract
def test_vocabulary_mapping_rejects_path_traversal_canonical_ids() -> None:
    payload = manifest().model_dump(mode="json")
    payload["vocabulary"]["class_definitions"][0][
        "canonical_class_id"
    ] = "../asset"
    payload["vocabulary"]["class_definitions"][0][
        "class_iri"
    ] = "https://ontology.contoso.test/kg/..%2Fasset"
    payload["vocabulary"]["property_definitions"][0][
        "domain_canonical_class_ids"
    ] = ["../asset"]
    payload["vocabulary"]["relationship_definitions"][0][
        "source_canonical_class_ids"
    ] = ["../asset"]
    reseal_manifest_payload(payload)
    with pytest.raises(ValidationError, match="path traversal"):
        RdfProjectionManifest.model_validate(payload)


@pytest.mark.contract
@pytest.mark.parametrize(
    ("contract_factory", "mutation"),
    [
        (
            manifest,
            lambda payload: payload["source_authority"].__setitem__(
                "domain_contract_id",
                SECRET_VALUES[0],
            ),
        ),
        (
            artifact,
            lambda payload: payload.__setitem__(
                "access_policy_id",
                SECRET_VALUES[1],
            ),
        ),
        (
            validation_receipt,
            lambda payload: payload["shacl_validation"].__setitem__(
                "validator_id",
                SECRET_VALUES[2],
            ),
        ),
    ],
)
def test_recursive_secret_rejection_across_top_level_rdf_contracts(
    contract_factory: object,
    mutation: object,
) -> None:
    if contract_factory is artifact:
        model = artifact("turtle")
        model_type = RdfSerializationArtifact
    else:
        model = contract_factory()
        model_type = type(model)
    payload = model.model_dump(mode="json")
    mutation(payload)
    with pytest.raises(ValidationError, match="secret|credential|signed|transient"):
        model_type.model_validate(payload)

    nested = manifest().model_dump(mode="json")
    nested["external_alignments"][0]["approval_reference_id"] = SECRET_VALUES[3]
    with pytest.raises(ValidationError, match="secret|credential"):
        RdfProjectionManifest.model_validate(nested)


@pytest.mark.contract
@pytest.mark.parametrize("secret_value", SECRET_VALUES[4:])
@pytest.mark.parametrize("encoding_depth", [0, 1, 4, 8, 12])
def test_signed_url_families_and_nested_encodings_fail_closed(
    secret_value: str,
    encoding_depth: int,
) -> None:
    encoded = secret_value
    for _ in range(encoding_depth):
        encoded = quote(encoded, safe="")
    payload = manifest().model_dump(mode="json")
    payload["source_authority"]["l5a_projection_manifest_id"] = encoded
    with pytest.raises(
        ValidationError,
        match=(
            "secret|credential|signed|transient|nested URL encoding|"
            "unstable URL authority"
        ),
    ):
        RdfProjectionManifest.model_validate(payload)


@pytest.mark.contract
@pytest.mark.parametrize(
    "stable_namespace_id",
    [
        "authorization:policy",
        "credential:approval",
        "password:policy",
        "authentication:vocabulary",
    ],
)
def test_colon_delimited_namespace_ids_are_not_credential_assignments(
    stable_namespace_id: str,
) -> None:
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["approval_reference_id"] = stable_namespace_id
    assert (
        RdfExternalAlignment.model_validate(payload).approval_reference_id
        == stable_namespace_id
    )


@pytest.mark.contract
@pytest.mark.parametrize(
    "invalid_assignment",
    [
        "Authorization: Bearer secret-value",
        "password: secret-value",
        "api-key=secret-value",
        "client_secret=secret-value",
        "authentication = secret-value",
    ],
)
def test_headers_and_explicit_secret_assignments_fail_closed(
    invalid_assignment: str,
) -> None:
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["approval_reference_id"] = invalid_assignment
    with pytest.raises(ValidationError, match="credential"):
        RdfExternalAlignment.model_validate(payload)


@pytest.mark.contract
@pytest.mark.parametrize(
    "path_assignment",
    [
        "client_secret=secret-value",
        "password=secret-value",
        "token=secret-value",
        "api-key=secret-value",
        "password:%20secret-value",
    ],
)
@pytest.mark.parametrize("encoding_depth", [0, 1, 2])
def test_url_path_credential_assignments_fail_closed(
    path_assignment: str,
    encoding_depth: int,
) -> None:
    encoded = path_assignment
    for _ in range(encoding_depth):
        encoded = quote(encoded, safe="")
    marker = "PATH_SECRET_MARKER_51"
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["target_iri"] = (
        f"https://metadata.contoso.test/path/{encoded}/{marker}"
    )
    with pytest.raises(ValidationError) as captured:
        RdfExternalAlignment.model_validate(payload)
    assert marker not in str(captured.value)
    assert marker not in json.dumps(captured.value.errors(), default=str)


@pytest.mark.contract
def test_url_path_namespace_text_without_assignment_remains_valid() -> None:
    target = "https://metadata.contoso.test/path/credential:approval"
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["target_iri"] = target
    assert RdfExternalAlignment.model_validate(payload).target_iri == target


@pytest.mark.contract
def test_benign_namespace_query_values_remain_valid_metadata() -> None:
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["approval_reference_id"] = (
        "https://metadata.contoso.test/approval?ref=authorization:policy"
    )
    assert RdfExternalAlignment.model_validate(payload).approval_reference_id.endswith(
        "authorization:policy"
    )


@pytest.mark.contract
@pytest.mark.parametrize(
    "stable_unicode_iri",
    [
        "https://metadata.contoso.test/path℀item",
        "https://metadata.contoso.test/path/℁",
        "https://metadata.contoso.test/path?ref=℅",
        "https://metadata.contoso.test/path#⁇",
        "https://metadata.contoso.test/path⁇item",
    ],
)
def test_unicode_iri_path_query_and_fragment_identity_remains_valid(
    stable_unicode_iri: str,
) -> None:
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["target_iri"] = stable_unicode_iri
    assert (
        RdfExternalAlignment.model_validate(payload).target_iri
        == stable_unicode_iri
    )


@pytest.mark.contract
@pytest.mark.parametrize(
    "canonical_host_iri",
    [
        "https://e\u0301xample.test/path",
        "https://éxample.test/path",
    ],
)
def test_nfc_equivalent_idna_hosts_compare_by_canonical_a_label(
    canonical_host_iri: str,
) -> None:
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["target_iri"] = canonical_host_iri
    validated = RdfExternalAlignment.model_validate(payload)
    assert validated.target_iri == canonical_host_iri


@pytest.mark.contract
@pytest.mark.parametrize(
    "ip_host_iri",
    [
        "https://127.0.0.1/path",
        "https://[::1]/path",
        "https://[2001:db8::1]/path",
    ],
)
def test_valid_ipv4_and_ipv6_hosts_remain_supported(ip_host_iri: str) -> None:
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["target_iri"] = ip_host_iri
    assert RdfExternalAlignment.model_validate(payload).target_iri == ip_host_iri


@pytest.mark.contract
@pytest.mark.parametrize(
    "invalid_host",
    [
        "https://ｅxample.test/IDNA_MARKER_29",
        "https://℀.test/IDNA_MARKER_29",
        "https://a\u200db.test/IDNA_MARKER_29",
        "https://😀.test/IDNA_MARKER_29",
        "https://abcא.test/IDNA_MARKER_29",
        "https://xn--invalid-.test/IDNA_MARKER_29",
    ],
)
def test_compatibility_and_invalid_idna_hosts_fail_input_free(
    invalid_host: str,
) -> None:
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["target_iri"] = invalid_host
    with pytest.raises(ValidationError) as captured:
        RdfExternalAlignment.model_validate(payload)
    assert "IDNA_MARKER_29" not in str(captured.value)
    assert "IDNA_MARKER_29" not in json.dumps(
        captured.value.errors(),
        default=str,
    )


@pytest.mark.contract
@pytest.mark.parametrize(
    "invalid_url",
    [
        "https://[::1/path?ref=URL_MARKER_73",
        "https://storage.test:99999/path/URL_MARKER_73",
        "https://user:URL_MARKER_73@storage.test/path",
        "https://storage.test／URL_MARKER_73/path",
        "https://storage.test%EF%BC%8FURL_MARKER_73/path",
        "https%3A%2F%2Fstorage.test/URL_MARKER_73",
        "ｈｔｔｐｓ://storage.test/URL_MARKER_73",
        "https://user%EF%BC%A0URL_MARKER_73@storage.test/path",
    ],
)
def test_url_parser_failures_never_expose_rejected_input(
    invalid_url: str,
) -> None:
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["approval_reference_id"] = invalid_url
    with pytest.raises(ValidationError) as captured:
        RdfExternalAlignment.model_validate(payload)
    assert "URL_MARKER_73" not in str(captured.value)
    assert "URL_MARKER_73" not in json.dumps(captured.value.errors(), default=str)


@pytest.mark.contract
@pytest.mark.parametrize(
    "empty_host_iri",
    [
        "https://:443/EMPTY_HOST_MARKER_64",
        "https://user@/EMPTY_HOST_MARKER_64",
        "https:///EMPTY_HOST_MARKER_64",
    ],
)
def test_https_iri_requires_nonempty_canonical_host_input_free(
    empty_host_iri: str,
) -> None:
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["target_iri"] = empty_host_iri
    with pytest.raises(ValidationError) as captured:
        RdfExternalAlignment.model_validate(payload)
    assert "EMPTY_HOST_MARKER_64" not in str(captured.value)
    assert "EMPTY_HOST_MARKER_64" not in json.dumps(
        captured.value.errors(),
        default=str,
    )


@pytest.mark.contract
def test_sensitive_text_size_bounds_cover_nfkc_expansion_and_boundaries() -> None:
    payload = manifest().external_alignments[0].model_dump(mode="json")
    payload["approval_reference_id"] = "a" * 65_536
    assert len(
        RdfExternalAlignment.model_validate(payload).approval_reference_id
    ) == 65_536

    payload["approval_reference_id"] = "a" * 65_537
    with pytest.raises(ValidationError, match="safe validation size"):
        RdfExternalAlignment.model_validate(payload)

    payload["approval_reference_id"] = "\ufdfa" * 2_000
    with pytest.raises(ValidationError, match="safe validation size"):
        RdfExternalAlignment.model_validate(payload)


@pytest.mark.contract
@pytest.mark.parametrize(
    ("model_type", "payload_factory", "mutate"),
    [
        (
            RdfProjectionManifest,
            lambda: manifest().model_dump(mode="json"),
            lambda payload, secret: payload["source_authority"].__setitem__(
                "domain_contract_id", secret
            ),
        ),
        (
            RdfSerializationArtifact,
            lambda: artifact("turtle").model_dump(mode="json"),
            lambda payload, secret: payload.__setitem__("access_policy_id", secret),
        ),
        (
            RdfValidationReceipt,
            lambda: validation_receipt().model_dump(mode="json"),
            lambda payload, secret: payload["shacl_validation"].__setitem__(
                "validator_id", secret
            ),
        ),
        (
            RdfExternalAlignment,
            lambda: manifest().external_alignments[0].model_dump(mode="json"),
            lambda payload, secret: payload.__setitem__(
                "approval_reference_id", secret
            ),
        ),
    ],
)
def test_rdf_validation_errors_never_expose_rejected_input(
    model_type: object,
    payload_factory: object,
    mutate: object,
) -> None:
    marker = "DO_NOT_EXPOSE_7f9a"
    secret = f"https://storage.test/item?password={marker}"
    payload = payload_factory()
    mutate(payload, secret)
    with pytest.raises(ValidationError) as captured:
        model_type.model_validate(payload)
    assert marker not in str(captured.value)
    assert marker not in json.dumps(captured.value.errors(), default=str)


@pytest.mark.contract
def test_round_trip_drift_is_recorded_as_failure() -> None:
    receipt = validation_receipt(drift=True)
    assert not receipt.exact_round_trip_equivalent
    payload = receipt.model_dump(mode="json")
    payload["exact_round_trip_equivalent"] = True
    payload["validation_receipt_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "validation_receipt_hash"}
    )
    with pytest.raises(ValidationError, match="round-trip equivalence"):
        RdfValidationReceipt.model_validate(payload)


@pytest.mark.contract
def test_round_trip_receipt_rejects_duplicate_format_observations() -> None:
    receipt = validation_receipt()
    payload = receipt.model_dump(mode="json")
    duplicate = next(
        item
        for item in payload["observations"]
        if item["serialization_format"] == "rdf_xml"
        and item["exposure"] == "public_schema"
    )
    duplicate["serialization_format"] = "canonical_n_quads"
    with pytest.raises(ValidationError, match="unique format/exposure"):
        RdfValidationReceipt.model_validate(payload)


@pytest.mark.contract
def test_generic_valid_invalid_and_golden_fixtures() -> None:
    valid = FIXTURES / "valid" / "rdf-serialization-artifact-generic.json"
    invalid = FIXTURES / "invalid" / "rdf-serialization-artifact-label-identity.json"
    golden_json = (
        FIXTURES / "golden" / "rdf-serialization-artifact-generic.canonical.json"
    )
    golden_hash = FIXTURES / "golden" / "rdf-serialization-artifact-generic.sha256"

    parsed = parse_contract(valid.read_text(encoding="utf-8"))
    assert isinstance(parsed, RdfSerializationArtifact)
    assert canonical_json(parsed) == golden_json.read_text(encoding="utf-8").strip()
    assert canonical_sha256(parsed) == golden_hash.read_text(encoding="utf-8").strip()
    with pytest.raises((ValidationError, ValueError)):
        parse_contract(invalid.read_text(encoding="utf-8"))


@pytest.mark.contract
def test_schema_generation_is_additive_and_existing_schema_bytes_are_identical(
    tmp_path: Path,
) -> None:
    write_registered_schemas(tmp_path)
    new_paths = {
        "c0-rdf_projection_acceptance_bundle-1.0.0.schema.json",
        "c0-rdf_projection_manifest-1.0.0.schema.json",
        "c0-rdf_serialization_artifact-1.0.0.schema.json",
        "c0-rdf_validation_receipt-1.0.0.schema.json",
    }
    assert new_paths.issubset({path.name for path in tmp_path.glob("*.schema.json")})

    for generated in tmp_path.glob("*.schema.json"):
        if generated.name in new_paths:
            continue
        committed = SCHEMAS / generated.name
        assert committed.read_bytes() == generated.read_bytes()

    current_registry = json.loads((SCHEMAS / "registry.json").read_text(encoding="utf-8"))
    assert_pre_rdf_baseline(
        schema_dir=SCHEMAS,
        current_registry=current_registry,
        baseline=PRE_RDF_BASELINE,
    )
    assert PRE_RDF_BASELINE["registry_version"] == "1.6.0"
    assert current_registry["registry_version"] == "1.7.0"
    baseline_keys = {
        (entry["contract_kind"], entry["contract_version"])
        for entry in PRE_RDF_BASELINE["registry_entries"]
    }
    additive_keys = {
        (entry["contract_kind"], entry["contract_version"])
        for entry in current_registry["schemas"]
    } - baseline_keys
    assert additive_keys == {
        ("c0.rdf_projection_acceptance_bundle", "1.0.0"),
        ("c0.rdf_projection_manifest", "1.0.0"),
        ("c0.rdf_serialization_artifact", "1.0.0"),
        ("c0.rdf_validation_receipt", "1.0.0"),
    }
    assert len(current_registry["schemas"]) == len(
        PRE_RDF_BASELINE["registry_entries"]
    ) + 4


@pytest.mark.contract
def test_pre_rdf_baseline_detects_file_and_registry_tampering(
    tmp_path: Path,
) -> None:
    copied_schemas = tmp_path / "schemas"
    copied_schemas.mkdir()
    for filename in PRE_RDF_BASELINE["files"]:
        (copied_schemas / filename).write_bytes((SCHEMAS / filename).read_bytes())
    current_registry = json.loads((SCHEMAS / "registry.json").read_text(encoding="utf-8"))

    first_filename = next(iter(PRE_RDF_BASELINE["files"]))
    (copied_schemas / first_filename).write_bytes(b"tampered\n")
    with pytest.raises(AssertionError):
        assert_pre_rdf_baseline(
            schema_dir=copied_schemas,
            current_registry=current_registry,
            baseline=PRE_RDF_BASELINE,
        )

    (copied_schemas / first_filename).write_bytes(
        (SCHEMAS / first_filename).read_bytes()
    )
    tampered_registry = copy.deepcopy(current_registry)
    tampered_registry["schemas"][0]["schema_hash"] = "f" * 64
    with pytest.raises(AssertionError):
        assert_pre_rdf_baseline(
            schema_dir=copied_schemas,
            current_registry=tampered_registry,
            baseline=PRE_RDF_BASELINE,
        )

    duplicate_registry = copy.deepcopy(current_registry)
    duplicate_registry["schemas"].append(
        copy.deepcopy(duplicate_registry["schemas"][0])
    )
    with pytest.raises(AssertionError):
        assert_pre_rdf_baseline(
            schema_dir=copied_schemas,
            current_registry=duplicate_registry,
            baseline=PRE_RDF_BASELINE,
        )


@pytest.mark.contract
def test_invalid_fixture_has_no_accidental_unrelated_change() -> None:
    payload = json.loads(
        (FIXTURES / "invalid" / "rdf-serialization-artifact-label-identity.json")
        .read_text(encoding="utf-8")
    )
    valid = artifact("turtle").model_dump(mode="json")
    invalid = copy.deepcopy(valid)
    invalid["blank_node_policy"] = "labels_are_identity"
    assert payload == invalid


@pytest.mark.contract
@pytest.mark.parametrize(
    ("model_type", "payload_factory", "collection_path"),
    [
        (
            RdfProjectionManifest,
            lambda: manifest().model_dump(mode="json"),
            ("named_graphs",),
        ),
        (
            RdfProjectionManifest,
            lambda: manifest().model_dump(mode="json"),
            ("external_alignments",),
        ),
        (
            RdfProjectionManifest,
            lambda: manifest().model_dump(mode="json"),
            ("vocabulary", "class_definitions"),
        ),
        (
            RdfProjectionManifest,
            lambda: manifest().model_dump(mode="json"),
            ("vocabulary", "property_definitions"),
        ),
        (
            RdfProjectionManifest,
            lambda: manifest().model_dump(mode="json"),
            ("vocabulary", "relationship_definitions"),
        ),
        (
            RdfSerializationArtifact,
            lambda: artifact("turtle").model_dump(mode="json"),
            ("graph_bindings",),
        ),
        (
            RdfSerializationArtifact,
            lambda: artifact("turtle").model_dump(mode="json"),
            ("named_graph_ids",),
        ),
        (
            RdfValidationReceipt,
            lambda: validation_receipt().model_dump(mode="json"),
            ("observations",),
        ),
        (
            RdfProjectionAcceptanceBundle,
            lambda: acceptance_bundle().model_dump(mode="json"),
            ("serialization_artifacts",),
        ),
    ],
)
@pytest.mark.parametrize(
    "malformed_collection",
    [
        None,
        42,
        [None],
        [[]],
        ["MALFORMED_COLLECTION_MARKER"],
    ],
)
def test_malformed_collection_entries_raise_sanitized_validation_errors(
    model_type: object,
    payload_factory: object,
    collection_path: tuple[str, ...],
    malformed_collection: object,
) -> None:
    payload = payload_factory()
    target = payload
    for segment in collection_path[:-1]:
        target = target[segment]
    target[collection_path[-1]] = malformed_collection
    with pytest.raises(ValidationError) as captured:
        model_type.model_validate(payload)
    assert "MALFORMED_COLLECTION_MARKER" not in str(captured.value)
    assert "MALFORMED_COLLECTION_MARKER" not in json.dumps(
        captured.value.errors(),
        default=str,
    )
