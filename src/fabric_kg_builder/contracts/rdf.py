"""Behavior-free C0.RDF semantic interchange contracts."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from .base import (
    ContractModel,
    RequiredText,
    SemVer,
    Sha256,
    canonical_sha256,
    sorted_unique,
)
from .identity import CanonicalIdentityEnvelope
from .publication import PublicationAuthorityReferences

NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]

RdfGraphRole = Literal[
    "common_schema",
    "domain_schema",
    "shacl_shapes",
    "instances",
    "provenance_authority",
]
RdfFormat = Literal["turtle", "rdf_xml", "json_ld", "canonical_n_quads"]
RdfExposure = Literal["public_schema", "protected_dataset"]
RdfTermKind = Literal["object_property", "datatype_property"]
EndpointEncoding = Literal["single_rdfs_term", "named_owl_union", "shacl_or"]
ExternalAlignmentRelation = Literal["rdfs_see_also", "skos_exact_match", "owl_equivalent"]

_FORMAT_PROFILE = {
    "turtle": ("text/turtle", "RDF 1.1 Turtle"),
    "rdf_xml": ("application/rdf+xml", "RDF 1.1 XML Syntax"),
    "json_ld": ("application/ld+json", "JSON-LD 1.1"),
    "canonical_n_quads": ("application/n-quads", "RDF 1.1 N-Quads"),
}
_PUBLIC_GRAPH_ROLES = {"common_schema", "domain_schema", "shacl_shapes"}


def _https_iri(value: str, *, field_name: str, base: bool = False) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or (base and parsed.fragment)
    ):
        raise ValueError(f"{field_name} must be an absolute credential-free HTTPS IRI")
    if base and not value.endswith(("/", "#")):
        raise ValueError(f"{field_name} must end with '/' or '#'")
    return value


def _sorted_unique_models(value: object, *, key: str, field_name: str) -> object:
    if not isinstance(value, (list, tuple)):
        return value
    items = tuple(
        sorted(
            value,
            key=lambda item: (
                str(getattr(item, key))
                if hasattr(item, key)
                else str(item.get(key, ""))
            ),
        )
    )
    keys = [
        str(getattr(item, key)) if hasattr(item, key) else str(item.get(key, ""))
        for item in items
    ]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{field_name} must contain unique {key} values")
    return items


class RdfSourceAuthorityTuple(ContractModel):
    """Exact upstream authority tuple; RDF is never an independent authority."""

    authority: Literal["derived"] = "derived"
    semantic_serving_projection_id: RequiredText
    semantic_serving_projection_hash: Sha256
    l5a_projection_manifest_id: RequiredText
    l5a_projection_manifest_hash: Sha256
    publication_crosswalk_id: RequiredText
    publication_crosswalk_contract_version: Literal["1.1.0"] = "1.1.0"
    publication_crosswalk_schema_hash: Sha256
    publication_crosswalk_hash: Sha256
    ontology_projection_equivalence_id: RequiredText
    ontology_projection_equivalence_hash: Sha256
    graph_projection_equivalence_id: RequiredText
    graph_projection_equivalence_hash: Sha256
    search_projection_equivalence_id: RequiredText
    search_projection_equivalence_hash: Sha256
    domain_contract_id: RequiredText
    domain_contract_hash: Sha256
    hierarchy_hash: Sha256
    identity_policy_hash: Sha256
    relationship_policy_hash: Sha256
    k_policy_hash: Sha256
    publication_authority: PublicationAuthorityReferences


class RdfIriPolicy(ContractModel):
    namespace_governance_id: RequiredText
    namespace_governance_hash: Sha256
    ontology_base_iri: RequiredText
    instance_base_iri: RequiredText
    ontology_iri: RequiredText
    version_iri: RequiredText
    ontology_semantic_version: SemVer
    canonical_id_mapping: Literal["utf8_percent_encoded_path_segment"] = (
        "utf8_percent_encoded_path_segment"
    )
    noncanonical_node_policy: Literal["deterministic_skolem_iri"] = (
        "deterministic_skolem_iri"
    )
    labels_define_identity: Literal[False] = False

    @field_validator("ontology_base_iri", "instance_base_iri")
    @classmethod
    def _base_iri(cls, value: str, info: Any) -> str:
        return _https_iri(value, field_name=info.field_name, base=True)

    @field_validator("ontology_iri", "version_iri")
    @classmethod
    def _absolute_iri(cls, value: str, info: Any) -> str:
        return _https_iri(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _governed_namespaces(self) -> "RdfIriPolicy":
        if self.ontology_base_iri == self.instance_base_iri:
            raise ValueError("ontology and instance base IRIs must be distinct")
        if not self.ontology_iri.startswith(self.ontology_base_iri):
            raise ValueError("ontology_iri must be governed by ontology_base_iri")
        if not self.version_iri.startswith(self.ontology_base_iri):
            raise ValueError("version_iri must be governed by ontology_base_iri")
        if self.ontology_semantic_version not in self.version_iri:
            raise ValueError("version_iri must contain ontology_semantic_version")
        return self


class RdfNamedGraph(ContractModel):
    graph_id: RequiredText
    graph_iri: RequiredText
    graph_role: RdfGraphRole
    required: bool
    contains_schema_triples: bool
    contains_instance_or_evidence_triples: bool
    access_policy_id: RequiredText | None = None
    access_policy_hash: Sha256 | None = None

    @field_validator("graph_iri")
    @classmethod
    def _graph_iri(cls, value: str) -> str:
        return _https_iri(value, field_name="graph_iri")

    @model_validator(mode="after")
    def _graph_policy(self) -> "RdfNamedGraph":
        if self.graph_role in _PUBLIC_GRAPH_ROLES:
            if not self.contains_schema_triples or self.contains_instance_or_evidence_triples:
                raise ValueError("public schema graph roles may contain schema triples only")
        else:
            if self.contains_schema_triples:
                raise ValueError("instance/provenance graph roles cannot contain schema triples")
            if not self.contains_instance_or_evidence_triples:
                raise ValueError("instance/provenance graphs must declare their triple category")
            if self.access_policy_id is None:
                raise ValueError("protected graphs require access policy ID and hash")
        if (self.access_policy_id is None) != (self.access_policy_hash is None):
            raise ValueError("access policy ID and hash must be paired")
        return self


class RdfClassDefinition(ContractModel):
    canonical_class_id: RequiredText
    class_iri: RequiredText
    parent_canonical_class_ids: tuple[str, ...] = ()
    exact_key_property_ids: tuple[str, ...]

    @field_validator("parent_canonical_class_ids", "exact_key_property_ids", mode="before")
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @field_validator("class_iri")
    @classmethod
    def _iri(cls, value: str) -> str:
        return _https_iri(value, field_name="class_iri")


class RdfPropertyDefinition(ContractModel):
    canonical_property_id: RequiredText
    property_iri: RequiredText
    term_kind: RdfTermKind
    domain_canonical_class_ids: tuple[str, ...]
    range_canonical_ids: tuple[str, ...]
    value_type_iris: tuple[str, ...]
    endpoint_encoding: EndpointEncoding
    deterministic_endpoint_node_iri: RequiredText | None = None

    @field_validator(
        "domain_canonical_class_ids",
        "range_canonical_ids",
        "value_type_iris",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @field_validator("property_iri", "deterministic_endpoint_node_iri")
    @classmethod
    def _iri(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _https_iri(value, field_name=info.field_name)

    @field_validator("value_type_iris")
    @classmethod
    def _value_iris(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _https_iri(item, field_name="value_type_iris")
        return value

    @model_validator(mode="after")
    def _endpoints(self) -> "RdfPropertyDefinition":
        if not self.domain_canonical_class_ids:
            raise ValueError("property domain set must not be empty")
        if not self.range_canonical_ids and not self.value_type_iris:
            raise ValueError("property requires a canonical range or value type")
        endpoint_count = max(
            len(self.domain_canonical_class_ids),
            len(self.range_canonical_ids),
            len(self.value_type_iris),
        )
        if endpoint_count > 1 and self.endpoint_encoding == "single_rdfs_term":
            raise ValueError("multiple endpoints cannot use repeated RDFS domain/range")
        if self.endpoint_encoding == "named_owl_union":
            if self.deterministic_endpoint_node_iri is None:
                raise ValueError("named OWL union requires a deterministic node IRI")
        elif self.deterministic_endpoint_node_iri is not None:
            raise ValueError("deterministic endpoint node is only valid for named OWL union")
        if self.term_kind == "object_property" and self.value_type_iris:
            raise ValueError("object properties cannot declare literal value types")
        if self.term_kind == "datatype_property" and self.range_canonical_ids:
            raise ValueError("datatype properties cannot declare canonical class ranges")
        return self


class RdfVocabularyInventory(ContractModel):
    owl_profile: Literal["OWL 2 RL compatible derived vocabulary"] = (
        "OWL 2 RL compatible derived vocabulary"
    )
    class_definitions: tuple[RdfClassDefinition, ...]
    property_definitions: tuple[RdfPropertyDefinition, ...]
    class_id_set_hash: Sha256
    property_id_set_hash: Sha256
    hierarchy_hash: Sha256
    vocabulary_hash: Sha256

    @field_validator("class_definitions", mode="before")
    @classmethod
    def _classes(cls, value: object) -> object:
        return _sorted_unique_models(
            value, key="canonical_class_id", field_name="class_definitions"
        )

    @field_validator("property_definitions", mode="before")
    @classmethod
    def _properties(cls, value: object) -> object:
        return _sorted_unique_models(
            value, key="canonical_property_id", field_name="property_definitions"
        )

    @model_validator(mode="after")
    def _inventory(self) -> "RdfVocabularyInventory":
        class_ids = {item.canonical_class_id for item in self.class_definitions}
        for item in self.class_definitions:
            if not set(item.parent_canonical_class_ids).issubset(class_ids):
                raise ValueError("class parent IDs must resolve within the inventory")
            if not set(item.exact_key_property_ids).issubset(
                prop.canonical_property_id for prop in self.property_definitions
            ):
                raise ValueError("class key property IDs must resolve within the inventory")
        for item in self.property_definitions:
            if not set(item.domain_canonical_class_ids).issubset(class_ids):
                raise ValueError("property domain IDs must resolve within the inventory")
            if not set(item.range_canonical_ids).issubset(class_ids):
                raise ValueError("property range IDs must resolve within the inventory")
        expected_class_hash = canonical_sha256(sorted(class_ids))
        expected_property_hash = canonical_sha256(
            sorted(item.canonical_property_id for item in self.property_definitions)
        )
        if self.class_id_set_hash != expected_class_hash:
            raise ValueError("class_id_set_hash does not match exact class IDs")
        if self.property_id_set_hash != expected_property_hash:
            raise ValueError("property_id_set_hash does not match exact property IDs")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"vocabulary_hash"})
        )
        if self.vocabulary_hash != expected:
            raise ValueError("vocabulary_hash does not match RDF vocabulary inventory")
        return self


class RdfExternalAlignment(ContractModel):
    target_iri: RequiredText
    relation_kind: ExternalAlignmentRelation
    source_artifact_reference_id: RequiredText
    source_artifact_version: RequiredText
    source_artifact_hash: Sha256
    source_license_id: RequiredText
    approval_reference_id: RequiredText
    approval_hash: Sha256
    import_policy: Literal["metadata_only_no_import_or_fetch"] = (
        "metadata_only_no_import_or_fetch"
    )

    @field_validator("target_iri")
    @classmethod
    def _target(cls, value: str) -> str:
        return _https_iri(value, field_name="target_iri")


class RdfProjectionManifest(ContractModel):
    identity: CanonicalIdentityEnvelope
    rdf_projection_manifest_id: RequiredText
    source_authority: RdfSourceAuthorityTuple
    iri_policy: RdfIriPolicy
    named_graphs: tuple[RdfNamedGraph, ...]
    vocabulary: RdfVocabularyInventory
    external_alignments: tuple[RdfExternalAlignment, ...] = ()
    required_serialization_formats: tuple[RdfFormat, ...] = (
        "turtle",
        "rdf_xml",
        "canonical_n_quads",
    )
    full_source_quotes_forbidden: Literal[True] = True
    transient_or_signed_urls_forbidden: Literal[True] = True
    evidence_reference_policy: Literal["ids_hashes_and_prov_links_only"] = (
        "ids_hashes_and_prov_links_only"
    )
    required_member_authority: Literal["c0.required_member_manifest"] = (
        "c0.required_member_manifest"
    )
    projection_manifest_hash: Sha256

    @field_validator("named_graphs", mode="before")
    @classmethod
    def _graphs(cls, value: object) -> object:
        return _sorted_unique_models(value, key="graph_id", field_name="named_graphs")

    @field_validator("external_alignments", mode="before")
    @classmethod
    def _alignments(cls, value: object) -> object:
        return _sorted_unique_models(
            value, key="target_iri", field_name="external_alignments"
        )

    @field_validator("required_serialization_formats", mode="before")
    @classmethod
    def _formats(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="required_serialization_formats")
        return value

    @model_validator(mode="after")
    def _manifest(self) -> "RdfProjectionManifest":
        if self.identity.contract_kind != "c0.rdf_projection_manifest":
            raise ValueError("invalid RDF projection manifest contract_kind")
        if self.identity.domain_contract_hash != self.source_authority.domain_contract_hash:
            raise ValueError("domain contract hash differs from identity authority")
        graph_roles = {item.graph_role for item in self.named_graphs}
        if len(graph_roles) != len(self.named_graphs):
            raise ValueError("RDF graph roles must be unique")
        required_roles = {
            "common_schema",
            "domain_schema",
            "shacl_shapes",
            "provenance_authority",
        }
        if not required_roles.issubset(graph_roles):
            raise ValueError("RDF graph inventory is missing required graph roles")
        if any(
            not graph.graph_iri.startswith(self.iri_policy.ontology_base_iri)
            for graph in self.named_graphs
        ):
            raise ValueError("named graph IRIs must be governed by ontology_base_iri")
        if self.vocabulary.hierarchy_hash != self.source_authority.hierarchy_hash:
            raise ValueError("RDF vocabulary hierarchy differs from source authority")
        graph_iris = [item.graph_iri for item in self.named_graphs]
        if len(graph_iris) != len(set(graph_iris)):
            raise ValueError("named graph IRIs must be unique")
        formats = set(self.required_serialization_formats)
        if not {"turtle", "rdf_xml", "canonical_n_quads"}.issubset(formats):
            raise ValueError("Turtle, RDF/XML, and canonical N-Quads are required")
        if len(formats) != len(self.required_serialization_formats):
            raise ValueError("required serialization formats must be unique")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"projection_manifest_hash"})
        )
        if self.projection_manifest_hash != expected:
            raise ValueError("projection_manifest_hash does not match manifest")
        return self


class RdfSerializationArtifact(ContractModel):
    identity: CanonicalIdentityEnvelope
    rdf_serialization_artifact_id: RequiredText
    rdf_projection_manifest_id: RequiredText
    rdf_projection_manifest_hash: Sha256
    serialization_format: RdfFormat
    media_type: RequiredText
    w3c_syntax_version: RequiredText
    exposure: RdfExposure
    content_hash: Sha256
    byte_count: NonNegativeInt
    triple_count: NonNegativeInt
    graph_count: PositiveInt
    named_graph_ids: tuple[str, ...]
    canonical_id_set_hash: Sha256
    canonical_dataset_hash_algorithm: Literal["RDFC-1.0"] = "RDFC-1.0"
    canonical_dataset_hash: Sha256
    blank_node_policy: Literal["none_after_deterministic_skolemization"] = (
        "none_after_deterministic_skolemization"
    )
    access_policy_id: RequiredText | None = None
    access_policy_hash: Sha256 | None = None
    serialization_artifact_hash: Sha256

    @field_validator("named_graph_ids", mode="before")
    @classmethod
    def _graphs(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="named_graph_ids")
        return value

    @model_validator(mode="after")
    def _artifact(self) -> "RdfSerializationArtifact":
        if self.identity.contract_kind != "c0.rdf_serialization_artifact":
            raise ValueError("invalid RDF serialization artifact contract_kind")
        expected_profile = _FORMAT_PROFILE[self.serialization_format]
        if (self.media_type, self.w3c_syntax_version) != expected_profile:
            raise ValueError("media type or W3C syntax version does not match format")
        if len(self.named_graph_ids) != len(set(self.named_graph_ids)):
            raise ValueError("named graph IDs must be unique")
        if self.graph_count != len(self.named_graph_ids):
            raise ValueError("graph_count must equal exact named graph inventory")
        if (self.access_policy_id is None) != (self.access_policy_hash is None):
            raise ValueError("access policy ID and hash must be paired")
        if self.exposure == "protected_dataset" and self.access_policy_id is None:
            raise ValueError("protected RDF datasets require access policy references")
        if self.exposure == "public_schema" and self.access_policy_id is not None:
            raise ValueError("public schema artifacts cannot carry ACL principal policy")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"serialization_artifact_hash"})
        )
        if self.serialization_artifact_hash != expected:
            raise ValueError("serialization_artifact_hash does not match artifact")
        return self


class RdfSerializationObservation(ContractModel):
    rdf_serialization_artifact_id: RequiredText
    serialization_format: RdfFormat
    content_hash: Sha256
    canonical_dataset_hash: Sha256
    named_graph_ids: tuple[str, ...]
    triple_count: NonNegativeInt
    missing_triple_count: NonNegativeInt
    extra_triple_count: NonNegativeInt
    authority_reference_set_hash: Sha256
    base_iri_matches: bool
    label_identity_detected: bool
    unstable_blank_node_detected: bool

    @field_validator("named_graph_ids", mode="before")
    @classmethod
    def _graphs(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="named_graph_ids")
        return value


class RdfShaclValidationSummary(ContractModel):
    shapes_hash: Sha256
    conforms: bool
    violation_count: NonNegativeInt
    warning_count: NonNegativeInt
    info_count: NonNegativeInt
    validation_report_hash: Sha256
    validator_id: RequiredText
    validator_version: RequiredText
    constraint_profile: Literal[
        "canonical_identity_key_cardinality_endpoint_and_type"
    ] = "canonical_identity_key_cardinality_endpoint_and_type"
    membership_policy: Literal["validate_projection_do_not_recompute"] = (
        "validate_projection_do_not_recompute"
    )

    @model_validator(mode="after")
    def _conformance(self) -> "RdfShaclValidationSummary":
        if self.conforms != (self.violation_count == 0):
            raise ValueError("SHACL conforms must equal zero violation count")
        return self


class RdfValidationReceipt(ContractModel):
    identity: CanonicalIdentityEnvelope
    rdf_validation_receipt_id: RequiredText
    rdf_projection_manifest_id: RequiredText
    rdf_projection_manifest_hash: Sha256
    source_authority_hash: Sha256
    canonical_n_quads_artifact_id: RequiredText
    canonical_dataset_hash_algorithm: Literal["RDFC-1.0"] = "RDFC-1.0"
    canonical_dataset_hash: Sha256
    observations: tuple[RdfSerializationObservation, ...]
    shacl_validation: RdfShaclValidationSummary
    exact_round_trip_equivalent: bool
    validation_receipt_hash: Sha256

    @field_validator("observations", mode="before")
    @classmethod
    def _observations(cls, value: object) -> object:
        return _sorted_unique_models(
            value,
            key="rdf_serialization_artifact_id",
            field_name="observations",
        )

    @model_validator(mode="after")
    def _receipt(self) -> "RdfValidationReceipt":
        if self.identity.contract_kind != "c0.rdf_validation_receipt":
            raise ValueError("invalid RDF validation receipt contract_kind")
        formats = {item.serialization_format for item in self.observations}
        if len(formats) != len(self.observations):
            raise ValueError("round-trip observations must have unique formats")
        if not {"turtle", "rdf_xml", "canonical_n_quads"}.issubset(formats):
            raise ValueError("round-trip receipt requires all mandatory serializations")
        canonical = next(
            item
            for item in self.observations
            if item.serialization_format == "canonical_n_quads"
        )
        if canonical.rdf_serialization_artifact_id != self.canonical_n_quads_artifact_id:
            raise ValueError("canonical N-Quads artifact ID mismatch")
        failures = any(
            item.canonical_dataset_hash != self.canonical_dataset_hash
            or item.named_graph_ids != canonical.named_graph_ids
            or item.triple_count != canonical.triple_count
            or item.missing_triple_count != 0
            or item.extra_triple_count != 0
            or item.authority_reference_set_hash
            != canonical.authority_reference_set_hash
            or not item.base_iri_matches
            or item.label_identity_detected
            or item.unstable_blank_node_detected
            for item in self.observations
        )
        expected_equivalence = not failures and self.shacl_validation.conforms
        if self.exact_round_trip_equivalent != expected_equivalence:
            raise ValueError(
                "round-trip equivalence must fail on dataset, graph, triple, authority, "
                "base IRI, label identity, blank-node, or SHACL drift"
            )
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"validation_receipt_hash"})
        )
        if self.validation_receipt_hash != expected:
            raise ValueError("validation_receipt_hash does not match receipt")
        return self
