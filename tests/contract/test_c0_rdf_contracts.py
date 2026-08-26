"""Behavior-free C0.RDF contract, fixture, and additivity gates."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts import (
    CanonicalIdentityEnvelope,
    PublicationAuthorityReferences,
    RdfClassDefinition,
    RdfExternalAlignment,
    RdfIriPolicy,
    RdfNamedGraph,
    RdfProjectionManifest,
    RdfPropertyDefinition,
    RdfSerializationArtifact,
    RdfSerializationObservation,
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
SCHEMAS = Path(__file__).parents[2] / "src" / "fabric_kg_builder" / "contracts" / "schemas"
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
        publication_authority=publication_authority,
    )


def vocabulary() -> RdfVocabularyInventory:
    classes = (
        RdfClassDefinition(
            canonical_class_id="semantic-type:asset",
            class_iri="https://ontology.contoso.test/kg/class/asset",
            parent_canonical_class_ids=(),
            exact_key_property_ids=("property:asset:id",),
        ),
        RdfClassDefinition(
            canonical_class_id="semantic-type:facility",
            class_iri="https://ontology.contoso.test/kg/class/facility",
            parent_canonical_class_ids=(),
            exact_key_property_ids=("property:facility:id",),
        ),
    )
    properties = (
        RdfPropertyDefinition(
            canonical_property_id="property:asset:facility",
            property_iri="https://ontology.contoso.test/kg/property/asset-facility",
            term_kind="object_property",
            domain_canonical_class_ids=("semantic-type:asset",),
            range_canonical_ids=("semantic-type:facility",),
            value_type_iris=(),
            endpoint_encoding="single_rdfs_term",
            deterministic_endpoint_node_iri=None,
        ),
        RdfPropertyDefinition(
            canonical_property_id="property:asset:id",
            property_iri="https://ontology.contoso.test/kg/property/asset-id",
            term_kind="datatype_property",
            domain_canonical_class_ids=("semantic-type:asset",),
            range_canonical_ids=(),
            value_type_iris=("https://www.w3.org/2001/XMLSchema#string",),
            endpoint_encoding="single_rdfs_term",
            deterministic_endpoint_node_iri=None,
        ),
        RdfPropertyDefinition(
            canonical_property_id="property:facility:id",
            property_iri="https://ontology.contoso.test/kg/property/facility-id",
            term_kind="datatype_property",
            domain_canonical_class_ids=("semantic-type:facility",),
            range_canonical_ids=(),
            value_type_iris=("https://www.w3.org/2001/XMLSchema#string",),
            endpoint_encoding="single_rdfs_term",
            deterministic_endpoint_node_iri=None,
        ),
    )
    values = {
        "owl_profile": "OWL 2 RL compatible derived vocabulary",
        "class_definitions": classes,
        "property_definitions": properties,
        "class_id_set_hash": canonical_sha256(
            sorted(item.canonical_class_id for item in classes)
        ),
        "property_id_set_hash": canonical_sha256(
            sorted(item.canonical_property_id for item in properties)
        ),
        "hierarchy_hash": H["1"],
    }
    return RdfVocabularyInventory(
        **values,
        vocabulary_hash=canonical_sha256(values),
    )


def manifest() -> RdfProjectionManifest:
    graphs = (
        RdfNamedGraph(
            graph_id="graph:common-schema",
            graph_iri="https://ontology.contoso.test/kg/graph/common-schema",
            graph_role="common_schema",
            required=True,
            contains_schema_triples=True,
            contains_instance_or_evidence_triples=False,
        ),
        RdfNamedGraph(
            graph_id="graph:domain-schema",
            graph_iri="https://ontology.contoso.test/kg/graph/domain-schema",
            graph_role="domain_schema",
            required=True,
            contains_schema_triples=True,
            contains_instance_or_evidence_triples=False,
        ),
        RdfNamedGraph(
            graph_id="graph:provenance",
            graph_iri="https://ontology.contoso.test/kg/graph/provenance",
            graph_role="provenance_authority",
            required=True,
            contains_schema_triples=False,
            contains_instance_or_evidence_triples=True,
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
        ),
    )
    values = {
        "identity": identity("c0.rdf_projection_manifest"),
        "rdf_projection_manifest_id": "rdf-projection-manifest:generic",
        "source_authority": source_authority(),
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
        "vocabulary": vocabulary(),
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
) -> RdfSerializationArtifact:
    profile = {
        "turtle": ("text/turtle", "RDF 1.1 Turtle"),
        "rdf_xml": ("application/rdf+xml", "RDF 1.1 XML Syntax"),
        "json_ld": ("application/ld+json", "JSON-LD 1.1"),
        "canonical_n_quads": ("application/n-quads", "RDF 1.1 N-Quads"),
    }[rdf_format]
    values = {
        "identity": identity("c0.rdf_serialization_artifact"),
        "rdf_serialization_artifact_id": artifact_id or f"rdf-artifact:{rdf_format}",
        "rdf_projection_manifest_id": manifest().rdf_projection_manifest_id,
        "rdf_projection_manifest_hash": manifest().projection_manifest_hash,
        "serialization_format": rdf_format,
        "media_type": profile[0],
        "w3c_syntax_version": profile[1],
        "exposure": "protected_dataset",
        "content_hash": H["9"],
        "byte_count": 1024,
        "triple_count": 42,
        "graph_count": 4,
        "named_graph_ids": (
            "graph:common-schema",
            "graph:domain-schema",
            "graph:provenance",
            "graph:shapes",
        ),
        "canonical_id_set_hash": H["a"],
        "canonical_dataset_hash_algorithm": "RDFC-1.0",
        "canonical_dataset_hash": dataset_hash,
        "blank_node_policy": "none_after_deterministic_skolemization",
        "access_policy_id": "access-policy:rdf-protected",
        "access_policy_hash": H["5"],
    }
    return RdfSerializationArtifact(
        **values,
        serialization_artifact_hash=canonical_sha256(values),
    )


def validation_receipt(*, drift: bool = False) -> RdfValidationReceipt:
    formats = ("turtle", "rdf_xml", "canonical_n_quads")
    observations = tuple(
        RdfSerializationObservation(
            rdf_serialization_artifact_id=f"rdf-artifact:{rdf_format}",
            serialization_format=rdf_format,
            content_hash=H["9"],
            canonical_dataset_hash=H["7"] if drift and rdf_format == "turtle" else H["8"],
            named_graph_ids=(
                "graph:common-schema",
                "graph:domain-schema",
                "graph:provenance",
                "graph:shapes",
            ),
            triple_count=42,
            missing_triple_count=0,
            extra_triple_count=0,
            authority_reference_set_hash=H["6"],
            base_iri_matches=True,
            label_identity_detected=False,
            unstable_blank_node_detected=False,
        )
        for rdf_format in formats
    )
    observations = tuple(
        sorted(observations, key=lambda item: item.rdf_serialization_artifact_id)
    )
    values = {
        "identity": identity("c0.rdf_validation_receipt"),
        "rdf_validation_receipt_id": "rdf-validation-receipt:generic",
        "rdf_projection_manifest_id": manifest().rdf_projection_manifest_id,
        "rdf_projection_manifest_hash": manifest().projection_manifest_hash,
        "source_authority_hash": canonical_sha256(source_authority()),
        "canonical_n_quads_artifact_id": "rdf-artifact:canonical_n_quads",
        "canonical_dataset_hash_algorithm": "RDFC-1.0",
        "canonical_dataset_hash": H["8"],
        "observations": observations,
        "shacl_validation": RdfShaclValidationSummary(
            shapes_hash=H["5"],
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
    assert parse_contract(canonical_json(validation_receipt())) == validation_receipt()


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
    payload["observations"][1]["serialization_format"] = "canonical_n_quads"
    with pytest.raises(ValidationError, match="unique formats"):
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

    baseline_paths = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", "src/fabric_kg_builder/contracts/schemas"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    for path in baseline_paths:
        if path.endswith("/registry.json"):
            continue
        baseline = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            check=True,
            capture_output=True,
        ).stdout
        assert (Path(__file__).parents[2] / path).read_bytes() == baseline

    baseline_registry = json.loads(
        subprocess.run(
            [
                "git",
                "show",
                "HEAD:src/fabric_kg_builder/contracts/schemas/registry.json",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    current_registry = json.loads((SCHEMAS / "registry.json").read_text(encoding="utf-8"))
    current_entries = {
        (entry["contract_kind"], entry["contract_version"]): entry
        for entry in current_registry["schemas"]
    }
    for entry in baseline_registry["schemas"]:
        key = (entry["contract_kind"], entry["contract_version"])
        assert current_entries[key] == entry
    assert baseline_registry["registry_version"] == "1.6.0"
    assert current_registry["registry_version"] == "1.7.0"


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
