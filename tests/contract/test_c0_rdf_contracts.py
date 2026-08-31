"""Behavior-free C0.RDF contract, fixture, and additivity gates."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote, unquote

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts import (
    CanonicalIdentityEnvelope,
    PublicationAuthorityReferences,
    RdfClassDefinition,
    RdfExternalAlignment,
    RdfIriPolicy,
    RdfNamedGraph,
    AcceptedRdfProjection,
    RdfProjectionCandidateBundle,
    RdfPayloadVerificationContext,
    RdfProjectionManifest,
    RdfPropertyDefinition,
    RdfRelationshipDefinition,
    RdfSerializationArtifact,
    RdfSerializationObservation,
    RdfSerializedGraphBinding,
    RdfShaclValidationSummary,
    RdfSourceAuthorityTuple,
    RdfValidationReceipt,
    RdfVerifiedGraph,
    RdfVerifiedPayload,
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
PAYLOAD_FIXTURES = FIXTURES / "payloads"


def verify_canonical_n_quads(
    payload: bytes,
    context: RdfPayloadVerificationContext,
) -> RdfVerifiedPayload:
    text = payload.decode("utf-8")
    lines = tuple(line for line in text.splitlines() if line)
    graph_lines: dict[str, list[str]] = {}
    for line in lines:
        iris = re.findall(r"<([^>]+)>", line)
        graph_lines.setdefault(iris[-1], []).append(line)
    graph_id_by_iri = {
        f"{context.ontology_base_iri}graph/common-schema": "graph:common-schema",
        f"{context.ontology_base_iri}graph/domain-schema": "graph:domain-schema",
        f"{context.ontology_base_iri}graph/shapes": "graph:shapes",
        f"{context.ontology_base_iri}graph/provenance": "graph:provenance",
    }
    graphs = []
    for graph_iri, items in sorted(
        graph_lines.items(),
        key=lambda pair: graph_id_by_iri[pair[0]],
    ):
        graph_bytes = ("\n".join(items) + "\n").encode("utf-8")
        canonical_ids: set[str] = set()
        for line in items:
            for iri in re.findall(r"<([^>]+)>", line):
                for base in (
                    context.ontology_base_iri,
                    context.instance_base_iri,
                ):
                    if not iri.startswith(base):
                        continue
                    suffix = iri.removeprefix(base)
                    if suffix.startswith("graph/"):
                        continue
                    canonical_ids.add(unquote(suffix))
        graphs.append(
            RdfVerifiedGraph(
                graph_id=graph_id_by_iri[graph_iri],
                graph_iri=graph_iri,
                graph_hash=hashlib.sha256(graph_bytes).hexdigest(),
                triple_count=len(items),
                canonical_id_set_hash=canonical_sha256(sorted(canonical_ids)),
                canonical_id_count=len(canonical_ids),
            )
        )
    return RdfVerifiedPayload(
        canonical_dataset_hash=hashlib.sha256(payload).hexdigest(),
        triple_count=len(lines),
        graphs=tuple(graphs),
    )


PUBLIC_PAYLOAD = (PAYLOAD_FIXTURES / "public-schema.canonical.nq").read_bytes()
PROTECTED_PAYLOAD = (
    PAYLOAD_FIXTURES / "protected-dataset.canonical.nq"
).read_bytes()
PUBLIC_VERIFIED = verify_canonical_n_quads(
    PUBLIC_PAYLOAD,
    RdfPayloadVerificationContext(
        serialization_format="canonical_n_quads",
        media_type="application/n-quads",
        exposure="public_schema",
        ontology_base_iri="https://ontology.contoso.test/kg/",
        instance_base_iri="https://data.contoso.test/kg/",
    ),
)
PROTECTED_VERIFIED = verify_canonical_n_quads(
    PROTECTED_PAYLOAD,
    RdfPayloadVerificationContext(
        serialization_format="canonical_n_quads",
        media_type="application/n-quads",
        exposure="protected_dataset",
        ontology_base_iri="https://ontology.contoso.test/kg/",
        instance_base_iri="https://data.contoso.test/kg/",
    ),
)


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
        provenance_canonical_id_set_hash=PROTECTED_VERIFIED.graphs[
            0
        ].canonical_id_set_hash,
        provenance_canonical_id_count=PROTECTED_VERIFIED.graphs[
            0
        ].canonical_id_count,
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
    public_graphs = {item.graph_id: item for item in PUBLIC_VERIFIED.graphs}
    protected_graphs = {
        item.graph_id: item for item in PROTECTED_VERIFIED.graphs
    }
    graphs = (
        RdfNamedGraph(
            graph_id="graph:common-schema",
            graph_iri="https://ontology.contoso.test/kg/graph/common-schema",
            graph_role="common_schema",
            required=True,
            contains_schema_triples=True,
            contains_instance_or_evidence_triples=False,
            expected_graph_hash=public_graphs["graph:common-schema"].graph_hash,
            expected_triple_count=public_graphs["graph:common-schema"].triple_count,
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
            expected_graph_hash=public_graphs["graph:domain-schema"].graph_hash,
            expected_triple_count=public_graphs["graph:domain-schema"].triple_count,
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
            expected_graph_hash=protected_graphs["graph:provenance"].graph_hash,
            expected_triple_count=protected_graphs["graph:provenance"].triple_count,
            canonical_id_set_hash=protected_graphs[
                "graph:provenance"
            ].canonical_id_set_hash,
            canonical_id_count=protected_graphs[
                "graph:provenance"
            ].canonical_id_count,
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
            expected_graph_hash=public_graphs["graph:shapes"].graph_hash,
            expected_triple_count=public_graphs["graph:shapes"].triple_count,
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
        "public_schema_canonical_n_quads_artifact_id": (
            "rdf-artifact:canonical_n_quads:public_schema"
        ),
        "protected_dataset_canonical_n_quads_artifact_id": (
            "rdf-artifact:canonical_n_quads:protected_dataset"
        ),
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
    dataset_hash: str | None = None,
    artifact_id: str | None = None,
    exposure: str = "protected_dataset",
) -> RdfSerializationArtifact:
    if dataset_hash is None:
        dataset_hash = H["8"] if exposure == "public_schema" else H["7"]
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
        "content_hash": dataset_hash if rdf_format == "canonical_n_quads" else H["9"],
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
            canonical_dataset_hash=(
                H["6"]
                if drift
                and rdf_format == "turtle"
                and exposure == "public_schema"
                else artifacts[(rdf_format, exposure)].canonical_dataset_hash
            ),
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
        "public_schema_canonical_dataset_hash": H["8"],
        "protected_dataset_canonical_dataset_hash": H["7"],
        "required_serialization_formats": projection.required_serialization_formats,
        "observations": observations,
        "shacl_validation": RdfShaclValidationSummary(
            shapes_hash=next(
                item.expected_graph_hash
                for item in projection.named_graphs
                if item.graph_role == "shacl_shapes"
            ),
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


def acceptance_bundle() -> RdfProjectionCandidateBundle:
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
        "identity": identity("c0.rdf_projection_candidate_bundle"),
        "rdf_projection_candidate_bundle_id": "rdf-candidate-bundle:generic",
        "authority_reference_set_hash": projection.authority_reference_set_hash,
        "canonical_id_partition_binding_hash": (
            receipt.canonical_id_partition_binding_hash
        ),
        "manifest": projection,
        "serialization_artifacts": artifacts,
        "validation_receipt": receipt,
        "candidate_status": "candidate",
    }
    return RdfProjectionCandidateBundle(
        **values,
        candidate_bundle_hash=canonical_sha256(values),
    )


def payload_candidate_bundle() -> tuple[RdfProjectionCandidateBundle, dict[str, bytes]]:
    payload = acceptance_bundle().model_dump(mode="json")
    actual_payloads: dict[str, bytes] = {}
    canonical_payloads = {
        "public_schema": (
            FIXTURES / "payloads" / "public-schema.canonical.nq"
        ).read_bytes(),
        "protected_dataset": (
            FIXTURES / "payloads" / "protected-dataset.canonical.nq"
        ).read_bytes(),
    }
    partition_hashes = {
        exposure: hashlib.sha256(content).hexdigest()
        for exposure, content in canonical_payloads.items()
    }
    artifact_hashes: dict[str, str] = {}
    artifact_content_hashes: dict[str, str] = {}
    for artifact_payload in payload["serialization_artifacts"]:
        artifact_id = artifact_payload["rdf_serialization_artifact_id"]
        exposure = artifact_payload["exposure"]
        if artifact_payload["serialization_format"] == "canonical_n_quads":
            content = canonical_payloads[exposure]
        else:
            content = f"payload:{artifact_id}".encode("utf-8")
        actual_payloads[artifact_id] = content
        artifact_payload["content_hash"] = hashlib.sha256(content).hexdigest()
        artifact_payload["byte_count"] = len(content)
        artifact_payload["canonical_dataset_hash"] = partition_hashes[exposure]
        artifact_payload["serialization_artifact_hash"] = canonical_sha256(
            {
                key: value
                for key, value in artifact_payload.items()
                if key != "serialization_artifact_hash"
            }
        )
        artifact_hashes[artifact_id] = artifact_payload[
            "serialization_artifact_hash"
        ]
        artifact_content_hashes[artifact_id] = artifact_payload["content_hash"]
    receipt = payload["validation_receipt"]
    receipt["public_schema_canonical_dataset_hash"] = partition_hashes[
        "public_schema"
    ]
    receipt["protected_dataset_canonical_dataset_hash"] = partition_hashes[
        "protected_dataset"
    ]
    for observation in receipt["observations"]:
        artifact_id = observation["rdf_serialization_artifact_id"]
        observation["rdf_serialization_artifact_hash"] = artifact_hashes[artifact_id]
        observation["content_hash"] = artifact_content_hashes[artifact_id]
        observation["canonical_dataset_hash"] = partition_hashes[
            observation["exposure"]
        ]
    receipt["validation_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt.items()
            if key != "validation_receipt_hash"
        }
    )
    payload["candidate_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "candidate_bundle_hash"
        }
    )
    return RdfProjectionCandidateBundle.model_validate(payload), actual_payloads

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
    payload["candidate_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "candidate_bundle_hash"
        }
    )
    with pytest.raises(ValidationError, match="artifact hash mismatch"):
        RdfProjectionCandidateBundle.model_validate(payload)

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
    payload["candidate_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "candidate_bundle_hash"
        }
    )
    with pytest.raises(ValidationError, match="manifest hash mismatch"):
        RdfProjectionCandidateBundle.model_validate(payload)

    payload = accepted.model_dump(mode="json")
    payload["manifest"]["required_serialization_formats"].append("json_ld")
    reseal_manifest_payload(payload["manifest"])
    payload["candidate_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "candidate_bundle_hash"
        }
    )
    with pytest.raises(ValidationError, match="manifest hash mismatch"):
        RdfProjectionCandidateBundle.model_validate(payload)


@pytest.mark.contract
def test_payload_acceptance_uses_actual_bytes_as_trust_root() -> None:
    candidate, payloads = payload_candidate_bundle()
    accepted = candidate.accept_payloads(
        payloads,
        canonical_n_quads_verifier=verify_canonical_n_quads,
    )
    assert isinstance(accepted, AcceptedRdfProjection)
    assert accepted.candidate_bundle_hash == candidate.candidate_bundle_hash

    swapped_payloads = dict(payloads)
    public_id = candidate.manifest.public_schema_canonical_n_quads_artifact_id
    protected_id = (
        candidate.manifest.protected_dataset_canonical_n_quads_artifact_id
    )
    swapped_payloads[public_id], swapped_payloads[protected_id] = (
        swapped_payloads[protected_id],
        swapped_payloads[public_id],
    )
    with pytest.raises(ValueError, match="actual artifact byte hash mismatch"):
        candidate.accept_payloads(
            swapped_payloads,
            canonical_n_quads_verifier=verify_canonical_n_quads,
        )


@pytest.mark.contract
def test_coordinated_metadata_swap_is_not_payload_acceptance() -> None:
    candidate, payloads = payload_candidate_bundle()
    forged = candidate.model_dump(mode="json")
    receipt = forged["validation_receipt"]
    public_hash = receipt["public_schema_canonical_dataset_hash"]
    protected_hash = receipt["protected_dataset_canonical_dataset_hash"]
    receipt["public_schema_canonical_dataset_hash"] = protected_hash
    receipt["protected_dataset_canonical_dataset_hash"] = public_hash
    artifact_hashes: dict[str, str] = {}
    for artifact_payload in forged["serialization_artifacts"]:
        swapped_hash = (
            protected_hash
            if artifact_payload["exposure"] == "public_schema"
            else public_hash
        )
        artifact_payload["canonical_dataset_hash"] = swapped_hash
        if artifact_payload["serialization_format"] == "canonical_n_quads":
            artifact_payload["content_hash"] = swapped_hash
        artifact_payload["serialization_artifact_hash"] = canonical_sha256(
            {
                key: value
                for key, value in artifact_payload.items()
                if key != "serialization_artifact_hash"
            }
        )
        artifact_hashes[artifact_payload["rdf_serialization_artifact_id"]] = (
            artifact_payload["serialization_artifact_hash"]
        )
    for observation in receipt["observations"]:
        observation["canonical_dataset_hash"] = (
            protected_hash
            if observation["exposure"] == "public_schema"
            else public_hash
        )
        observation["rdf_serialization_artifact_hash"] = artifact_hashes[
            observation["rdf_serialization_artifact_id"]
        ]
        if observation["serialization_format"] == "canonical_n_quads":
            observation["content_hash"] = observation["canonical_dataset_hash"]
    receipt["validation_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt.items()
            if key != "validation_receipt_hash"
        }
    )
    forged["candidate_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in forged.items()
            if key != "candidate_bundle_hash"
        }
    )
    self_consistent_candidate = RdfProjectionCandidateBundle.model_validate(forged)
    with pytest.raises(ValueError, match="actual artifact byte hash mismatch"):
        self_consistent_candidate.accept_payloads(
            payloads,
            canonical_n_quads_verifier=verify_canonical_n_quads,
        )


@pytest.mark.contract
def test_payload_verifier_interface_has_no_expected_answers() -> None:
    candidate, payloads = payload_candidate_bundle()
    contexts = []

    def capture_context(
        payload: bytes,
        context: RdfPayloadVerificationContext,
    ) -> RdfVerifiedPayload:
        contexts.append(context)
        return verify_canonical_n_quads(payload, context)

    candidate.accept_payloads(
        payloads,
        canonical_n_quads_verifier=capture_context,
    )
    assert contexts
    assert set(contexts[0].__dict__) == {
        "serialization_format",
        "media_type",
        "exposure",
        "ontology_base_iri",
        "instance_base_iri",
    }


@pytest.mark.contract
@pytest.mark.parametrize("mutation", ["missing", "extra", "swapped", "same_count"])
def test_per_graph_payload_verification_rejects_mutations(mutation: str) -> None:
    candidate, payloads = payload_candidate_bundle()

    def malicious_verifier(
        payload: bytes,
        context: RdfPayloadVerificationContext,
    ) -> RdfVerifiedPayload:
        verified = verify_canonical_n_quads(payload, context).model_dump(mode="json")
        if context.exposure != "public_schema":
            return RdfVerifiedPayload.model_validate(verified)
        if mutation == "missing":
            verified["graphs"].pop()
            verified["triple_count"] = sum(
                item["triple_count"] for item in verified["graphs"]
            )
        elif mutation == "extra":
            extra = copy.deepcopy(verified["graphs"][0])
            extra["graph_id"] = "graph:extra"
            extra["graph_iri"] = "https://ontology.contoso.test/kg/graph/extra"
            verified["graphs"].append(extra)
            verified["triple_count"] += extra["triple_count"]
        elif mutation == "swapped":
            verified["graphs"][0]["graph_id"], verified["graphs"][1]["graph_id"] = (
                verified["graphs"][1]["graph_id"],
                verified["graphs"][0]["graph_id"],
            )
        else:
            verified["graphs"][0]["graph_hash"] = H["f"]
        return RdfVerifiedPayload.model_validate(verified)

    with pytest.raises(
        ValueError,
        match="triple count mismatch|per-graph payload observations mismatch",
    ):
        candidate.accept_payloads(
            payloads,
            canonical_n_quads_verifier=malicious_verifier,
        )


@pytest.mark.contract
def test_payload_verifier_incomplete_result_and_exception_are_sanitized() -> None:
    candidate, payloads = payload_candidate_bundle()

    def aggregate_only(
        payload: bytes,
        context: RdfPayloadVerificationContext,
    ) -> dict[str, object]:
        del context
        return {
            "canonical_dataset_hash": hashlib.sha256(payload).hexdigest(),
            "triple_count": len(payload.splitlines()),
        }

    with pytest.raises(ValueError, match="verification failed") as invalid:
        candidate.accept_payloads(
            payloads,
            canonical_n_quads_verifier=aggregate_only,
        )
    assert "graphs" not in str(invalid.value)

    marker = "PROTECTED_TRIPLE_SECRET_93"

    def leaking_parser(
        payload: bytes,
        context: RdfPayloadVerificationContext,
    ) -> RdfVerifiedPayload:
        del payload, context
        raise RuntimeError(f"parser failed on {marker}")

    with pytest.raises(ValueError, match="verification failed") as captured:
        candidate.accept_payloads(
            payloads,
            canonical_n_quads_verifier=leaking_parser,
        )
    assert marker not in str(captured.value)


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
    public_only_bundle["candidate_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in public_only_bundle.items()
            if key != "candidate_bundle_hash"
        }
    )
    with pytest.raises(ValidationError, match="exact artifact set"):
        RdfProjectionCandidateBundle.model_validate(public_only_bundle)

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
        "identity": identity("c0.rdf_projection_candidate_bundle"),
        "rdf_projection_candidate_bundle_id": "rdf-candidate-bundle:forged-shapes",
        "authority_reference_set_hash": projection.authority_reference_set_hash,
        "canonical_id_partition_binding_hash": (
            receipt.canonical_id_partition_binding_hash
        ),
        "manifest": projection,
        "serialization_artifacts": tuple(artifacts),
        "validation_receipt": receipt,
        "candidate_status": "candidate",
    }
    with pytest.raises(ValidationError, match="SHACL shapes hash"):
        RdfProjectionCandidateBundle(
            **bundle_values,
            candidate_bundle_hash=canonical_sha256(bundle_values),
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
    payload["candidate_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "candidate_bundle_hash"
        }
    )
    with pytest.raises(
        ValidationError,
        match="authority reference set hash mismatch",
    ):
        RdfProjectionCandidateBundle.model_validate(payload)


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
    payload["candidate_bundle_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "candidate_bundle_hash"
        }
    )
    with pytest.raises(
        ValidationError,
        match="canonical ID binding differs from manifest",
    ):
        RdfProjectionCandidateBundle.model_validate(payload)


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
def test_partition_dataset_hashes_are_distinct_and_exposure_bound() -> None:
    receipt = validation_receipt()
    assert receipt.public_schema_canonical_dataset_hash == H["8"]
    assert receipt.protected_dataset_canonical_dataset_hash == H["7"]

    reused = receipt.model_dump(mode="json")
    reused["protected_dataset_canonical_dataset_hash"] = H["8"]
    with pytest.raises(ValidationError, match="must differ"):
        RdfValidationReceipt.model_validate(reused)

    swapped = receipt.model_dump(mode="json")
    swapped["public_schema_canonical_dataset_hash"] = H["7"]
    swapped["protected_dataset_canonical_dataset_hash"] = H["8"]
    with pytest.raises(ValidationError, match="canonical N-Quads hashes"):
        RdfValidationReceipt.model_validate(swapped)

    missing = receipt.model_dump(mode="json")
    del missing["protected_dataset_canonical_dataset_hash"]
    with pytest.raises(ValidationError, match="protected_dataset"):
        RdfValidationReceipt.model_validate(missing)

    drifted = receipt.model_dump(mode="json")
    observation = next(
        item
        for item in drifted["observations"]
        if item["serialization_format"] == "rdf_xml"
        and item["exposure"] == "protected_dataset"
    )
    observation["canonical_dataset_hash"] = H["6"]
    drifted["exact_round_trip_equivalent"] = False
    drifted["validation_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in drifted.items()
            if key != "validation_receipt_hash"
        }
    )
    assert not RdfValidationReceipt.model_validate(
        drifted
    ).exact_round_trip_equivalent


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
        "c0-rdf_projection_candidate_bundle-1.0.0.schema.json",
        "c0-rdf_projection_manifest-1.0.0.schema.json",
        "c0-rdf_serialization_artifact-1.0.0.schema.json",
        "c0-rdf_validation_receipt-1.0.0.schema.json",
        "c0-projection_equivalence-1.1.0.schema.json",
        "c0-publication_crosswalk-1.2.0.schema.json",
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
    assert current_registry["registry_version"] == "1.8.0"
    baseline_keys = {
        (entry["contract_kind"], entry["contract_version"])
        for entry in PRE_RDF_BASELINE["registry_entries"]
    }
    additive_keys = {
        (entry["contract_kind"], entry["contract_version"])
        for entry in current_registry["schemas"]
    } - baseline_keys
    assert additive_keys == {
        ("c0.rdf_projection_candidate_bundle", "1.0.0"),
        ("c0.rdf_projection_manifest", "1.0.0"),
        ("c0.rdf_serialization_artifact", "1.0.0"),
        ("c0.rdf_validation_receipt", "1.0.0"),
        ("c0.projection_equivalence", "1.1.0"),
        ("c0.publication_crosswalk", "1.2.0"),
    }
    assert len(current_registry["schemas"]) == len(
        PRE_RDF_BASELINE["registry_entries"]
    ) + 6


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
            RdfProjectionCandidateBundle,
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
