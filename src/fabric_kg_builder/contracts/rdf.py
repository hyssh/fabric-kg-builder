"""Behavior-free C0.RDF semantic interchange contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, quote, unquote, urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from .base import (
    ContractModel,
    RequiredText,
    SemVer,
    Sha256,
    canonical_sha256,
    reject_secret_text,
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
_PROTECTED_GRAPH_ROLES = {"instances", "provenance_authority"}
_SIGNED_QUERY_KEYS = {
    "sig",
    "se",
    "sp",
    "sv",
    "spr",
    "st",
    "token",
    "access_token",
    "api_key",
    "apikey",
    "client_secret",
    "accountkey",
    "sharedaccesssignature",
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-algorithm",
    "x-goog-signature",
    "x-goog-credential",
}
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:authorization\s*[:=]\s*bearer|bearer\s+\S+|"
    r"(?:api[_-]?key|x-api-key|sig(?:nature)?|token|secret|credential|"
    r"client_secret|accountkey|sharedaccesssignature)\s*[:=]\s*\S+)"
)
_MAX_DECODE_ROUNDS = 12
_MAX_SENSITIVE_TEXT_BYTES = 65_536


def _https_iri(value: str, *, field_name: str, base: bool = False) -> str:
    _reject_sensitive_text(value, field_name=field_name)
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


def _reject_sensitive_text(value: str, *, field_name: str) -> None:
    if len(value.encode("utf-8")) > _MAX_SENSITIVE_TEXT_BYTES:
        raise ValueError(f"{field_name} exceeds the safe validation size")
    decoded = value
    stable = False
    for _ in range(_MAX_DECODE_ROUNDS):
        expanded = unquote(decoded)
        if expanded == decoded:
            stable = True
            break
        if len(expanded.encode("utf-8")) > _MAX_SENSITIVE_TEXT_BYTES:
            raise ValueError(f"{field_name} exceeds the safe validation size")
        decoded = expanded
    if not stable:
        raise ValueError(f"{field_name} contains excessive nested URL encoding")
    reject_secret_text(decoded, field_name=field_name)
    if _SECRET_VALUE_RE.search(decoded):
        raise ValueError(f"{field_name} must not contain secrets or credentials")
    parsed = urlparse(decoded)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain URI credentials")
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query)}
    normalized_keys = {
        re.sub(r"[-_]", "", key.casefold())
        for key in query_keys
    }
    normalized_signed = {
        re.sub(r"[-_]", "", key.casefold())
        for key in _SIGNED_QUERY_KEYS
    }
    generic_signed = any(
        "signature" in key
        or "credential" in key
        or "securitytoken" in key
        or key.endswith("token")
        for key in normalized_keys
    )
    if query_keys.intersection(_SIGNED_QUERY_KEYS) or normalized_keys.intersection(
        normalized_signed
    ) or generic_signed:
        raise ValueError(f"{field_name} must not contain a signed or transient URL")


def _reject_sensitive_in(value: Any, *, path: str = "contract") -> None:
    if isinstance(value, str):
        _reject_sensitive_text(value, field_name=path)
        return
    if isinstance(value, BaseModel):
        for name, item in value.__dict__.items():
            _reject_sensitive_in(item, path=f"{path}.{name}")
        return
    if isinstance(value, Mapping):
        for name, item in value.items():
            _reject_sensitive_in(name, path=f"{path}.key")
            _reject_sensitive_in(item, path=f"{path}.{name}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            _reject_sensitive_in(item, path=f"{path}[{index}]")


def _canonical_term_iri(base_iri: str, canonical_id: str) -> str:
    if re.search(r"(^|[\\/])\.{1,2}([\\/]|$)", canonical_id):
        raise ValueError("canonical IDs used for RDF IRIs must not contain path traversal")
    return base_iri + quote(canonical_id, safe="", encoding="utf-8", errors="strict")


def _canonical_union_node_iri(
    base_iri: str,
    *,
    term_id: str,
    side: str,
    endpoint_ids: Sequence[str],
) -> str:
    endpoint_set_hash = canonical_sha256(sorted(endpoint_ids))
    return _canonical_term_iri(
        base_iri,
        f"endpoint-union:{term_id}:{side}:{endpoint_set_hash}",
    )


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
    canonical_iri_mapping_version: Literal["1.0"] = "1.0"
    canonical_id_mapping: Literal["utf8_percent_encoded_path_segment"] = (
        "utf8_percent_encoded_path_segment"
    )
    instance_iri_mapping_version: Literal["1.0"] = "1.0"
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
    deterministic_domain_union_node_iri: RequiredText | None = None
    deterministic_range_union_node_iri: RequiredText | None = None

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

    @field_validator(
        "property_iri",
        "deterministic_domain_union_node_iri",
        "deterministic_range_union_node_iri",
    )
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
        range_ids = (*self.range_canonical_ids, *self.value_type_iris)
        has_multi_domain = len(self.domain_canonical_class_ids) > 1
        has_multi_range = len(range_ids) > 1
        if (has_multi_domain or has_multi_range) and self.endpoint_encoding == (
            "single_rdfs_term"
        ):
            raise ValueError("multiple endpoints cannot use repeated RDFS domain/range")
        if not has_multi_domain and not has_multi_range and self.endpoint_encoding != (
            "single_rdfs_term"
        ):
            raise ValueError("single endpoints must use direct RDFS class/value IRIs")
        for side, has_multiple, node_iri in (
            (
                "domain",
                has_multi_domain,
                self.deterministic_domain_union_node_iri,
            ),
            (
                "range",
                has_multi_range,
                self.deterministic_range_union_node_iri,
            ),
        ):
            if has_multiple != (node_iri is not None):
                raise ValueError(
                    f"deterministic {side} union node is required iff {side} has "
                    "multiple endpoints"
                )
        if (
            self.deterministic_domain_union_node_iri is not None
            and self.deterministic_domain_union_node_iri
            == self.deterministic_range_union_node_iri
        ):
            raise ValueError("domain and range union nodes must be distinct")
        if self.term_kind == "object_property" and self.value_type_iris:
            raise ValueError("object properties cannot declare literal value types")
        if self.term_kind == "datatype_property" and self.range_canonical_ids:
            raise ValueError("datatype properties cannot declare canonical class ranges")
        return self


class RdfRelationshipDefinition(ContractModel):
    canonical_relationship_id: RequiredText
    relationship_iri: RequiredText
    source_canonical_class_ids: tuple[str, ...]
    target_canonical_class_ids: tuple[str, ...]
    endpoint_encoding: EndpointEncoding
    deterministic_source_union_node_iri: RequiredText | None = None
    deterministic_target_union_node_iri: RequiredText | None = None

    @field_validator(
        "source_canonical_class_ids",
        "target_canonical_class_ids",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @field_validator(
        "relationship_iri",
        "deterministic_source_union_node_iri",
        "deterministic_target_union_node_iri",
    )
    @classmethod
    def _iri(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _https_iri(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _endpoints(self) -> "RdfRelationshipDefinition":
        if not self.source_canonical_class_ids or not self.target_canonical_class_ids:
            raise ValueError("relationship source and target sets must not be empty")
        has_multi_source = len(self.source_canonical_class_ids) > 1
        has_multi_target = len(self.target_canonical_class_ids) > 1
        if (has_multi_source or has_multi_target) and self.endpoint_encoding == (
            "single_rdfs_term"
        ):
            raise ValueError("multiple endpoints cannot use repeated RDFS domain/range")
        if not has_multi_source and not has_multi_target and self.endpoint_encoding != (
            "single_rdfs_term"
        ):
            raise ValueError("single endpoints must use direct class IRIs")
        for side, has_multiple, node_iri in (
            (
                "source",
                has_multi_source,
                self.deterministic_source_union_node_iri,
            ),
            (
                "target",
                has_multi_target,
                self.deterministic_target_union_node_iri,
            ),
        ):
            if has_multiple != (node_iri is not None):
                raise ValueError(
                    f"deterministic {side} union node is required iff {side} has "
                    "multiple endpoints"
                )
        if (
            self.deterministic_source_union_node_iri is not None
            and self.deterministic_source_union_node_iri
            == self.deterministic_target_union_node_iri
        ):
            raise ValueError("source and target union nodes must be distinct")
        return self


class RdfVocabularyInventory(ContractModel):
    owl_profile: Literal["OWL 2 RL compatible derived vocabulary"] = (
        "OWL 2 RL compatible derived vocabulary"
    )
    class_definitions: tuple[RdfClassDefinition, ...]
    property_definitions: tuple[RdfPropertyDefinition, ...]
    relationship_definitions: tuple[RdfRelationshipDefinition, ...]
    class_id_set_hash: Sha256
    property_id_set_hash: Sha256
    relationship_id_set_hash: Sha256
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

    @field_validator("relationship_definitions", mode="before")
    @classmethod
    def _relationships(cls, value: object) -> object:
        return _sorted_unique_models(
            value,
            key="canonical_relationship_id",
            field_name="relationship_definitions",
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
        for item in self.relationship_definitions:
            if not set(item.source_canonical_class_ids).issubset(class_ids):
                raise ValueError("relationship source IDs must resolve within inventory")
            if not set(item.target_canonical_class_ids).issubset(class_ids):
                raise ValueError("relationship target IDs must resolve within inventory")
        expected_class_hash = canonical_sha256(sorted(class_ids))
        expected_property_hash = canonical_sha256(
            sorted(item.canonical_property_id for item in self.property_definitions)
        )
        expected_relationship_hash = canonical_sha256(
            sorted(
                item.canonical_relationship_id
                for item in self.relationship_definitions
            )
        )
        if self.class_id_set_hash != expected_class_hash:
            raise ValueError("class_id_set_hash does not match exact class IDs")
        if self.property_id_set_hash != expected_property_hash:
            raise ValueError("property_id_set_hash does not match exact property IDs")
        if self.relationship_id_set_hash != expected_relationship_hash:
            raise ValueError(
                "relationship_id_set_hash does not match exact relationship IDs"
            )
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
        _reject_sensitive_in(self)
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
            not graph.required
            for graph in self.named_graphs
            if graph.graph_role in required_roles
        ):
            raise ValueError("mandatory RDF graph roles must declare required=true")
        instance_graph = next(
            (
                graph
                for graph in self.named_graphs
                if graph.graph_role == "instances"
            ),
            None,
        )
        if instance_graph is not None and not instance_graph.required:
            raise ValueError(
                "an instances graph, when declared, must be required by the dataset"
            )
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
        term_bindings = [
            (item.canonical_class_id, item.class_iri)
            for item in self.vocabulary.class_definitions
        ] + [
            (item.canonical_property_id, item.property_iri)
            for item in self.vocabulary.property_definitions
        ] + [
            (item.canonical_relationship_id, item.relationship_iri)
            for item in self.vocabulary.relationship_definitions
        ]
        canonical_ids = [canonical_id for canonical_id, _ in term_bindings]
        term_iris = [iri for _, iri in term_bindings]
        if len(canonical_ids) != len(set(canonical_ids)):
            raise ValueError("canonical RDF vocabulary IDs must be globally unique")
        if len(term_iris) != len(set(term_iris)):
            raise ValueError("RDF vocabulary term IRIs must be globally injective")
        for canonical_id, iri in term_bindings:
            expected_iri = _canonical_term_iri(
                self.iri_policy.ontology_base_iri,
                canonical_id,
            )
            if iri != expected_iri:
                raise ValueError(
                    "RDF vocabulary term IRI must exactly equal the governed "
                    "canonical-ID mapping"
                )
        for prop in self.vocabulary.property_definitions:
            for side, endpoint_ids, node_iri in (
                (
                    "domain",
                    prop.domain_canonical_class_ids,
                    prop.deterministic_domain_union_node_iri,
                ),
                (
                    "range",
                    (*prop.range_canonical_ids, *prop.value_type_iris),
                    prop.deterministic_range_union_node_iri,
                ),
            ):
                if node_iri is None:
                    continue
                expected_node_iri = _canonical_union_node_iri(
                    self.iri_policy.ontology_base_iri,
                    term_id=prop.canonical_property_id,
                    side=side,
                    endpoint_ids=endpoint_ids,
                )
                if node_iri != expected_node_iri:
                    raise ValueError(
                        "endpoint union IRI must use the deterministic governed mapping"
                    )
        for relationship in self.vocabulary.relationship_definitions:
            for side, endpoint_ids, node_iri in (
                (
                    "source",
                    relationship.source_canonical_class_ids,
                    relationship.deterministic_source_union_node_iri,
                ),
                (
                    "target",
                    relationship.target_canonical_class_ids,
                    relationship.deterministic_target_union_node_iri,
                ),
            ):
                if node_iri is None:
                    continue
                expected_node_iri = _canonical_union_node_iri(
                    self.iri_policy.ontology_base_iri,
                    term_id=relationship.canonical_relationship_id,
                    side=side,
                    endpoint_ids=endpoint_ids,
                )
                if node_iri != expected_node_iri:
                    raise ValueError(
                        "endpoint union IRI must use the deterministic governed mapping"
                    )
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


class RdfSerializedGraphBinding(ContractModel):
    graph_id: RequiredText
    graph_iri: RequiredText
    graph_role: RdfGraphRole
    required: bool
    triple_count: NonNegativeInt
    graph_hash: Sha256
    access_policy_id: RequiredText | None = None
    access_policy_hash: Sha256 | None = None

    @field_validator("graph_iri")
    @classmethod
    def _graph_iri(cls, value: str) -> str:
        return _https_iri(value, field_name="graph_iri")

    @model_validator(mode="after")
    def _binding(self) -> "RdfSerializedGraphBinding":
        if (self.access_policy_id is None) != (self.access_policy_hash is None):
            raise ValueError("graph access policy ID and hash must be paired")
        if self.graph_role in _PROTECTED_GRAPH_ROLES and self.access_policy_id is None:
            raise ValueError("protected graph roles require access policy ID and hash")
        if self.graph_role in _PUBLIC_GRAPH_ROLES and self.access_policy_id is not None:
            raise ValueError("public schema graph roles cannot carry ACL policy")
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
    graph_bindings: tuple[RdfSerializedGraphBinding, ...]
    graph_inventory_hash: Sha256
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

    @field_validator("graph_bindings", mode="before")
    @classmethod
    def _graph_bindings(cls, value: object) -> object:
        return _sorted_unique_models(
            value,
            key="graph_id",
            field_name="graph_bindings",
        )

    @model_validator(mode="after")
    def _artifact(self) -> "RdfSerializationArtifact":
        _reject_sensitive_in(self)
        if self.identity.contract_kind != "c0.rdf_serialization_artifact":
            raise ValueError("invalid RDF serialization artifact contract_kind")
        expected_profile = _FORMAT_PROFILE[self.serialization_format]
        if (self.media_type, self.w3c_syntax_version) != expected_profile:
            raise ValueError("media type or W3C syntax version does not match format")
        binding_ids = tuple(item.graph_id for item in self.graph_bindings)
        if self.named_graph_ids != binding_ids:
            raise ValueError("named graph IDs must exactly equal sealed graph bindings")
        if self.graph_count != len(self.graph_bindings):
            raise ValueError("graph_count must equal exact named graph inventory")
        if self.triple_count != sum(item.triple_count for item in self.graph_bindings):
            raise ValueError("triple_count must equal sealed graph binding totals")
        expected_graph_hash = canonical_sha256(self.graph_bindings)
        if self.graph_inventory_hash != expected_graph_hash:
            raise ValueError("graph_inventory_hash does not match graph bindings")
        if (self.access_policy_id is None) != (self.access_policy_hash is None):
            raise ValueError("access policy ID and hash must be paired")
        if self.exposure == "protected_dataset" and self.access_policy_id is None:
            raise ValueError("protected RDF datasets require access policy references")
        if self.exposure == "public_schema" and self.access_policy_id is not None:
            raise ValueError("public schema artifacts cannot carry ACL principal policy")
        roles = {item.graph_role for item in self.graph_bindings}
        if len(roles) != len(self.graph_bindings):
            raise ValueError("serialized graph roles must be unique")
        if self.exposure == "public_schema" and roles != _PUBLIC_GRAPH_ROLES:
            raise ValueError(
                "public schema artifacts require exactly the three public graph roles"
            )
        if self.exposure == "protected_dataset":
            mandatory_roles = _PUBLIC_GRAPH_ROLES | {"provenance_authority"}
            if not mandatory_roles.issubset(roles) or not roles.issubset(
                mandatory_roles | {"instances"}
            ):
                raise ValueError(
                    "protected datasets require exact mandatory roles and optional instances"
                )
        if any(not item.required for item in self.graph_bindings):
            raise ValueError("every serialized graph binding must be required")
        if roles.intersection(_PROTECTED_GRAPH_ROLES):
            if self.exposure != "protected_dataset":
                raise ValueError("protected graph roles require protected exposure")
            if any(
                (item.access_policy_id, item.access_policy_hash)
                != (self.access_policy_id, self.access_policy_hash)
                for item in self.graph_bindings
                if item.graph_role in _PROTECTED_GRAPH_ROLES
            ):
                raise ValueError(
                    "protected graph policies must equal artifact access policy"
                )
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"serialization_artifact_hash"})
        )
        if self.serialization_artifact_hash != expected:
            raise ValueError("serialization_artifact_hash does not match artifact")
        return self

    def validate_against_manifest(self, manifest: RdfProjectionManifest) -> None:
        if self.rdf_projection_manifest_id != manifest.rdf_projection_manifest_id:
            raise ValueError("RDF projection manifest ID mismatch")
        if self.rdf_projection_manifest_hash != manifest.projection_manifest_hash:
            raise ValueError("RDF projection manifest hash mismatch")
        if self.serialization_format not in manifest.required_serialization_formats:
            raise ValueError("serialization format is not declared by the manifest")
        expected_graphs = tuple(
            graph
            for graph in manifest.named_graphs
            if self.exposure == "protected_dataset"
            or graph.graph_role in _PUBLIC_GRAPH_ROLES
        )
        actual = tuple(
            (
                item.graph_id,
                item.graph_iri,
                item.graph_role,
                item.required,
                item.access_policy_id,
                item.access_policy_hash,
            )
            for item in self.graph_bindings
        )
        expected = tuple(
            (
                item.graph_id,
                item.graph_iri,
                item.graph_role,
                item.required,
                item.access_policy_id,
                item.access_policy_hash,
            )
            for item in expected_graphs
        )
        if actual != expected:
            raise ValueError(
                "artifact graph IDs, roles, requirements, and policies must exactly "
                "match manifest inventory"
            )


class RdfSerializationObservation(ContractModel):
    rdf_serialization_artifact_id: RequiredText
    rdf_serialization_artifact_hash: Sha256
    serialization_format: RdfFormat
    media_type: RequiredText
    content_hash: Sha256
    canonical_dataset_hash: Sha256
    named_graph_ids: tuple[str, ...]
    graph_inventory_hash: Sha256
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
    required_serialization_formats: tuple[RdfFormat, ...]
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

    @field_validator("required_serialization_formats", mode="before")
    @classmethod
    def _formats(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="required_serialization_formats")
        return value

    @model_validator(mode="after")
    def _receipt(self) -> "RdfValidationReceipt":
        _reject_sensitive_in(self)
        if self.identity.contract_kind != "c0.rdf_validation_receipt":
            raise ValueError("invalid RDF validation receipt contract_kind")
        formats = {item.serialization_format for item in self.observations}
        if len(formats) != len(self.observations):
            raise ValueError("round-trip observations must have unique formats")
        if formats != set(self.required_serialization_formats):
            raise ValueError(
                "observation formats must exactly equal required serialization formats"
            )
        if "canonical_n_quads" not in formats:
            raise ValueError("round-trip receipt requires canonical N-Quads")
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

    def validate_against_manifest_and_artifacts(
        self,
        manifest: RdfProjectionManifest,
        artifacts: Sequence[RdfSerializationArtifact],
    ) -> None:
        if self.rdf_projection_manifest_id != manifest.rdf_projection_manifest_id:
            raise ValueError("RDF projection manifest ID mismatch")
        if self.rdf_projection_manifest_hash != manifest.projection_manifest_hash:
            raise ValueError("RDF projection manifest hash mismatch")
        if self.source_authority_hash != canonical_sha256(manifest.source_authority):
            raise ValueError("RDF source authority hash mismatch")
        if self.required_serialization_formats != manifest.required_serialization_formats:
            raise ValueError("receipt required formats differ from manifest")
        artifact_by_id = {
            item.rdf_serialization_artifact_id: item for item in artifacts
        }
        if len(artifact_by_id) != len(artifacts):
            raise ValueError("RDF serialization artifact IDs must be unique")
        observation_ids = {
            item.rdf_serialization_artifact_id for item in self.observations
        }
        if set(artifact_by_id) != observation_ids:
            raise ValueError("receipt observations must bind the exact artifact set")
        for observation in self.observations:
            artifact = artifact_by_id[observation.rdf_serialization_artifact_id]
            artifact.validate_against_manifest(manifest)
            checks = (
                (
                    "artifact hash",
                    observation.rdf_serialization_artifact_hash,
                    artifact.serialization_artifact_hash,
                ),
                ("format", observation.serialization_format, artifact.serialization_format),
                ("media type", observation.media_type, artifact.media_type),
                ("content hash", observation.content_hash, artifact.content_hash),
                (
                    "canonical dataset hash",
                    observation.canonical_dataset_hash,
                    artifact.canonical_dataset_hash,
                ),
                (
                    "graph inventory hash",
                    observation.graph_inventory_hash,
                    artifact.graph_inventory_hash,
                ),
                ("named graph IDs", observation.named_graph_ids, artifact.named_graph_ids),
                ("triple count", observation.triple_count, artifact.triple_count),
            )
            for name, observed, sealed in checks:
                if observed != sealed:
                    raise ValueError(f"receipt observation {name} mismatch")


class RdfProjectionAcceptanceBundle(ContractModel):
    """Self-contained proof that a complete RDF projection is accepted."""

    identity: CanonicalIdentityEnvelope
    rdf_projection_acceptance_bundle_id: RequiredText
    manifest: RdfProjectionManifest
    serialization_artifacts: tuple[RdfSerializationArtifact, ...]
    validation_receipt: RdfValidationReceipt
    acceptance_status: Literal["accepted"] = "accepted"
    acceptance_bundle_hash: Sha256

    @field_validator("serialization_artifacts", mode="before")
    @classmethod
    def _artifacts(cls, value: object) -> object:
        return _sorted_unique_models(
            value,
            key="rdf_serialization_artifact_id",
            field_name="serialization_artifacts",
        )

    @model_validator(mode="after")
    def _accepted_bundle(self) -> "RdfProjectionAcceptanceBundle":
        _reject_sensitive_in(self)
        if self.identity.contract_kind != "c0.rdf_projection_acceptance_bundle":
            raise ValueError("invalid RDF projection acceptance bundle contract_kind")
        authority_identity = self.manifest.identity.model_dump(
            mode="json",
            exclude={"contract_kind", "contract_version"},
        )
        for name, candidate in (
            ("bundle", self.identity),
            *(
                (f"artifact {item.rdf_serialization_artifact_id}", item.identity)
                for item in self.serialization_artifacts
            ),
            ("receipt", self.validation_receipt.identity),
        ):
            candidate_identity = candidate.model_dump(
                mode="json",
                exclude={"contract_kind", "contract_version"},
            )
            if candidate_identity != authority_identity:
                raise ValueError(f"{name} identity differs from manifest authority")
        self.validation_receipt.validate_against_manifest_and_artifacts(
            self.manifest,
            self.serialization_artifacts,
        )
        if not self.validation_receipt.shacl_validation.conforms:
            raise ValueError("accepted RDF bundle requires SHACL conformance")
        if not self.validation_receipt.exact_round_trip_equivalent:
            raise ValueError("accepted RDF bundle requires exact round-trip equivalence")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"acceptance_bundle_hash"})
        )
        if self.acceptance_bundle_hash != expected:
            raise ValueError("acceptance_bundle_hash does not match accepted RDF bundle")
        return self
