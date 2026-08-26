"""Behavior-free C0.RDF semantic interchange contracts."""

from __future__ import annotations

import re
import unicodedata
from ipaddress import ip_address
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal
from urllib.parse import ParseResult, parse_qsl, quote, unquote, urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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
    "password",
    "passwd",
    "pwd",
    "auth",
    "authentication",
    "authorization",
}
_CREDENTIAL_KEY_PATTERN = (
    r"(?:api[\s._-]*key|x[\s._-]*api[\s._-]*key|sig(?:nature)?|token|"
    r"secret|credential|password|passwd|pwd|auth(?:entication|orization)?|"
    r"client[\s._-]*secret|account[\s._-]*key|shared[\s._-]*access"
    r"[\s._-]*signature|x[\s._-]*amz[\s._-]*(?:signature|credential|"
    r"security[\s._-]*token|algorithm)|x[\s._-]*goog[\s._-]*"
    r"(?:signature|credential))"
)
_BEARER_RE = re.compile(
    r"(?i)(?:authorization\s*:\s*bearer\s+\S+|\bbearer\s+\S+)"
)
_EQUALS_ASSIGNMENT_RE = re.compile(
    rf"(?i)\b{_CREDENTIAL_KEY_PATTERN}\s*=\s*\S+"
)
_HEADER_ASSIGNMENT_RE = re.compile(
    rf"(?i)\b{_CREDENTIAL_KEY_PATTERN}\s*:\s+\S+"
)
_MAX_DECODE_ROUNDS = 12
_MAX_SENSITIVE_TEXT_BYTES = 65_536


def _https_iri(value: str, *, field_name: str, base: bool = False) -> str:
    _reject_sensitive_text(value, field_name=field_name)
    parsed = _safe_urlparse(value, field_name=field_name)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or (base and (parsed.query or parsed.fragment))
    ):
        raise ValueError(f"{field_name} must be an absolute credential-free HTTPS IRI")
    if base and not value.endswith(("/", "#")):
        raise ValueError(f"{field_name} must end with '/' or '#'")
    return value


def _safe_urlparse(value: str, *, field_name: str) -> ParseResult:
    try:
        parsed = urlparse(value)
        # Force validation of netloc components that urlparse exposes lazily.
        _ = parsed.hostname
        _ = parsed.port
        _ = parsed.username
        _ = parsed.password
        return parsed
    except (TypeError, ValueError, UnicodeError):
        raise ValueError(f"{field_name} contains invalid URL syntax") from None


def _check_sensitive_size(value: str, *, field_name: str) -> None:
    if len(value.encode("utf-8")) > _MAX_SENSITIVE_TEXT_BYTES:
        raise ValueError(f"{field_name} exceeds the safe validation size")


def _canonical_host(hostname: str, *, field_name: str) -> str:
    try:
        return ip_address(hostname).compressed.casefold()
    except ValueError:
        pass
    try:
        nfc_hostname = unicodedata.normalize("NFC", hostname)
        if unicodedata.normalize("NFKC", nfc_hostname) != nfc_hostname:
            raise ValueError
        candidate = nfc_hostname.rstrip(".")
        if not candidate:
            raise ValueError
        for character in candidate:
            category = unicodedata.category(character)
            if character not in {".", "-"} and category[0] not in {"L", "M", "N"}:
                raise ValueError
        labels = candidate.split(".")
        if any(not label for label in labels):
            raise ValueError
        a_labels: list[str] = []
        for label in labels:
            a_label = label.encode("idna").decode("ascii").casefold()
            if not 1 <= len(a_label.encode("ascii")) <= 63:
                raise ValueError
            decoded_label = a_label.encode("ascii").decode("idna")
            if (
                decoded_label.encode("idna").decode("ascii").casefold()
                != a_label
            ):
                raise ValueError
            a_labels.append(a_label)
        canonical = ".".join(a_labels)
        if len(canonical.encode("ascii")) > 253:
            raise ValueError
        return canonical
    except (UnicodeError, ValueError):
        raise ValueError(f"{field_name} contains invalid URL authority") from None


def _authority_signature(
    parsed: ParseResult,
    *,
    field_name: str,
) -> tuple[Any, ...] | None:
    if not parsed.scheme or not parsed.netloc:
        return None
    return (
        parsed.scheme.casefold(),
        (
            _canonical_host(parsed.hostname, field_name=field_name)
            if parsed.hostname is not None
            else None
        ),
        parsed.port,
        parsed.username,
        parsed.password,
    )


def _safe_parse_qsl(value: str, *, field_name: str) -> list[tuple[str, str]]:
    try:
        return parse_qsl(value, keep_blank_values=True)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError(f"{field_name} contains invalid URL syntax") from None


def _reject_sensitive_text(value: str, *, field_name: str) -> None:
    _check_sensitive_size(value, field_name=field_name)
    raw_parsed = _safe_urlparse(value, field_name=field_name)
    raw_authority = _authority_signature(raw_parsed, field_name=field_name)
    decoded = unicodedata.normalize("NFKC", value)
    _check_sensitive_size(decoded, field_name=field_name)
    stable = False
    for _ in range(_MAX_DECODE_ROUNDS):
        unescaped = unquote(decoded)
        expanded = unicodedata.normalize("NFKC", unescaped)
        if expanded == decoded:
            stable = True
            break
        _check_sensitive_size(expanded, field_name=field_name)
        decoded = expanded
    if not stable:
        raise ValueError(f"{field_name} contains excessive nested URL encoding")
    decoded = unicodedata.normalize("NFKC", decoded)
    _check_sensitive_size(decoded, field_name=field_name)
    parsed = _safe_urlparse(decoded, field_name=field_name)
    if _authority_signature(parsed, field_name=field_name) != raw_authority:
        raise ValueError(f"{field_name} contains unstable URL authority syntax")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain URI credentials")
    if _BEARER_RE.search(decoded):
        raise ValueError(f"{field_name} must not contain bearer credentials")

    def normalize_key(key: str) -> str:
        return re.sub(
            r"[^a-z0-9]",
            "",
            unicodedata.normalize("NFKC", key).casefold(),
        )

    def component_keys(value: str) -> set[str]:
        keys: set[str] = set()
        for segment in re.split(r"[&;]", unicodedata.normalize("NFKC", value)):
            match = re.match(r"^\s*([^=:\s]+)\s*(?:=|:\s+)", segment)
            if match is not None:
                keys.add(normalize_key(match.group(1)))
        return keys

    query_items = (
        _safe_parse_qsl(parsed.query, field_name=field_name)
        if parsed.scheme and parsed.netloc
        else ()
    )
    normalized_keys = {
        normalize_key(key) for key, _ in query_items
    } | component_keys(parsed.query)
    normalized_signed = {
        normalize_key(key)
        for key in _SIGNED_QUERY_KEYS
    }
    credential_keys = {
        "password",
        "passwd",
        "pwd",
        "auth",
        "authentication",
        "authorization",
        "clientsecret",
    }
    generic_signed = any(
        "signature" in key
        or "credential" in key
        or "securitytoken" in key
        or key.endswith("token")
        for key in normalized_keys
    )
    if normalized_keys.intersection(normalized_signed) or generic_signed:
        raise ValueError(f"{field_name} must not contain a signed or transient URL")
    if normalized_keys.intersection(credential_keys):
        raise ValueError(f"{field_name} must not contain credentials")
    normalized_query = unicodedata.normalize("NFKC", parsed.query)
    if (
        _EQUALS_ASSIGNMENT_RE.search(normalized_query)
        or _HEADER_ASSIGNMENT_RE.search(normalized_query)
    ):
        raise ValueError(f"{field_name} must not contain credentials")

    for _, query_value in query_items:
        normalized_value = unicodedata.normalize("NFKC", query_value)
        if (
            _BEARER_RE.search(normalized_value)
            or _EQUALS_ASSIGNMENT_RE.search(normalized_value)
            or _HEADER_ASSIGNMENT_RE.search(normalized_value)
        ):
            raise ValueError(f"{field_name} must not contain credentials")

    text_to_check = decoded
    if parsed.scheme and parsed.netloc:
        text_to_check = unicodedata.normalize("NFKC", parsed.fragment)
        fragment_keys = {
            normalize_key(key)
            for key, _ in _safe_parse_qsl(
                parsed.fragment,
                field_name=field_name,
            )
        } | component_keys(parsed.fragment)
        generic_fragment_key = any(
            "signature" in key
            or "credential" in key
            or "securitytoken" in key
            or key.endswith("token")
            for key in fragment_keys
        )
        if (
            fragment_keys.intersection(normalized_signed | credential_keys)
            or generic_fragment_key
        ):
            raise ValueError(f"{field_name} must not contain credentials")
        normalized_path = unicodedata.normalize("NFKC", parsed.path)
        if (
            _EQUALS_ASSIGNMENT_RE.search(normalized_path)
            or _HEADER_ASSIGNMENT_RE.search(normalized_path)
        ):
            raise ValueError(f"{field_name} must not contain credentials")
    if (
        _EQUALS_ASSIGNMENT_RE.search(text_to_check)
        or _HEADER_ASSIGNMENT_RE.search(text_to_check)
    ):
        raise ValueError(f"{field_name} must not contain credentials")


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
    if any(
        not isinstance(item, (BaseModel, Mapping))
        or (
            isinstance(item, BaseModel)
            and not hasattr(item, key)
        )
        for item in value
    ):
        return tuple(value)

    def key_value(item: BaseModel | Mapping[Any, Any]) -> str:
        if isinstance(item, BaseModel):
            return str(getattr(item, key))
        return str(item.get(key, ""))

    items = tuple(
        sorted(
            value,
            key=key_value,
        )
    )
    keys = [key_value(item) for item in items]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{field_name} must contain unique {key} values")
    return items


def _sanitized_validation_error(
    model_name: str,
    error: ValidationError,
) -> ValidationError:
    details = error.errors(include_input=False, include_url=False)
    return ValidationError.from_exception_data(model_name, details)


def _is_canonically_equivalent_host_iri(value: str) -> bool:
    normalized = unicodedata.normalize("NFC", value)
    if normalized == value:
        return True
    try:
        raw = _safe_urlparse(value, field_name="contract string")
        nfc = _safe_urlparse(normalized, field_name="contract string")
        if not raw.scheme or not raw.netloc:
            return False
        if (
            raw.scheme != nfc.scheme
            or raw.path != nfc.path
            or raw.params != nfc.params
            or raw.query != nfc.query
            or raw.fragment != nfc.fragment
        ):
            return False
        return _authority_signature(
            raw,
            field_name="contract string",
        ) == _authority_signature(
            nfc,
            field_name="contract string",
        )
    except ValueError:
        return False


class RdfContractModel(ContractModel):
    """RDF-local strict model that never exposes rejected input in errors."""

    model_config = ConfigDict(hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_sensitive_input(cls, value: Any) -> Any:
        _reject_sensitive_in(value)
        return value

    @model_validator(mode="after")
    def _require_unicode_nfc(self) -> "RdfContractModel":
        def check(value: Any) -> None:
            if isinstance(value, str):
                if not _is_canonically_equivalent_host_iri(value):
                    raise ValueError(
                        "contract strings must be Unicode NFC except for "
                        "canonically equivalent IRI host spelling"
                    )
                return
            if isinstance(value, BaseModel):
                for item in value.__dict__.values():
                    check(item)
                return
            if isinstance(value, Mapping):
                for key, item in value.items():
                    check(key)
                    check(item)
                return
            if isinstance(value, Sequence) and not isinstance(
                value,
                (bytes, bytearray),
            ):
                for item in value:
                    check(item)

        check(self)
        return self

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as error:
            raise _sanitized_validation_error(type(self).__name__, error) from None

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Any:
        try:
            return super().model_validate(obj, **kwargs)
        except ValidationError as error:
            raise _sanitized_validation_error(cls.__name__, error) from None

    @classmethod
    def model_validate_json(cls, json_data: str | bytes | bytearray, **kwargs: Any) -> Any:
        try:
            return super().model_validate_json(json_data, **kwargs)
        except ValidationError as error:
            raise _sanitized_validation_error(cls.__name__, error) from None


class RdfSourceAuthorityTuple(RdfContractModel):
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
    instance_canonical_id_set_hash: Sha256
    instance_canonical_id_count: NonNegativeInt
    provenance_canonical_id_set_hash: Sha256
    provenance_canonical_id_count: NonNegativeInt
    publication_authority: PublicationAuthorityReferences

    def reference_set_hash(self) -> str:
        references = (
            {
                "reference_name": "semantic_serving_projection",
                "reference_id": self.semantic_serving_projection_id,
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.semantic_serving_projection_hash,
            },
            {
                "reference_name": "l5a_projection_manifest",
                "reference_id": self.l5a_projection_manifest_id,
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.l5a_projection_manifest_hash,
            },
            {
                "reference_name": "publication_crosswalk",
                "reference_id": self.publication_crosswalk_id,
                "contract_version": self.publication_crosswalk_contract_version,
                "schema_hash": self.publication_crosswalk_schema_hash,
                "content_hash": self.publication_crosswalk_hash,
            },
            {
                "reference_name": "ontology_projection_equivalence",
                "reference_id": self.ontology_projection_equivalence_id,
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.ontology_projection_equivalence_hash,
            },
            {
                "reference_name": "graph_projection_equivalence",
                "reference_id": self.graph_projection_equivalence_id,
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.graph_projection_equivalence_hash,
            },
            {
                "reference_name": "search_projection_equivalence",
                "reference_id": self.search_projection_equivalence_id,
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.search_projection_equivalence_hash,
            },
            {
                "reference_name": "domain_contract",
                "reference_id": self.domain_contract_id,
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.domain_contract_hash,
            },
            {
                "reference_name": "hierarchy_policy",
                "reference_id": "authority:hierarchy",
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.hierarchy_hash,
            },
            {
                "reference_name": "identity_policy",
                "reference_id": "authority:identity-policy",
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.identity_policy_hash,
            },
            {
                "reference_name": "relationship_policy",
                "reference_id": "authority:relationship-policy",
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.relationship_policy_hash,
            },
            {
                "reference_name": "k_policy",
                "reference_id": "authority:k-policy",
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.k_policy_hash,
            },
            {
                "reference_name": "instance_canonical_id_set",
                "reference_id": "authority:instance-canonical-id-set",
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.instance_canonical_id_set_hash,
                "canonical_id_count": self.instance_canonical_id_count,
            },
            {
                "reference_name": "provenance_canonical_id_set",
                "reference_id": "authority:provenance-canonical-id-set",
                "contract_version": None,
                "schema_hash": None,
                "content_hash": self.provenance_canonical_id_set_hash,
                "canonical_id_count": self.provenance_canonical_id_count,
            },
            {
                "reference_name": "required_member_manifest",
                "reference_id": (
                    self.publication_authority.required_member_manifest_id
                ),
                "contract_version": (
                    self.publication_authority.required_member_manifest_contract_version
                ),
                "schema_hash": (
                    self.publication_authority.required_member_manifest_schema_hash
                ),
                "content_hash": (
                    self.publication_authority.required_member_manifest_hash
                ),
            },
            {
                "reference_name": "authoritative_collection",
                "reference_id": (
                    self.publication_authority.required_member_manifest_id
                ),
                "contract_version": (
                    self.publication_authority.required_member_manifest_contract_version
                ),
                "schema_hash": None,
                "content_hash": (
                    self.publication_authority.authoritative_collection_hash
                ),
            },
            {
                "reference_name": "source_artifact_manifest",
                "reference_id": (
                    self.publication_authority.source_artifact_manifest_id
                ),
                "contract_version": None,
                "schema_hash": None,
                "content_hash": (
                    self.publication_authority.source_artifact_manifest_hash
                ),
            },
        )
        return canonical_sha256(
            tuple(sorted(references, key=lambda item: item["reference_name"]))
        )


class RdfIriPolicy(RdfContractModel):
    namespace_governance_id: RequiredText
    namespace_governance_hash: Sha256
    ontology_base_iri: RequiredText
    instance_base_iri: RequiredText
    ontology_iri: RequiredText
    version_iri: RequiredText
    ontology_semantic_version: SemVer
    hostname_normalization_profile: Literal[
        "c0.rdf.nfc-idna2003-strict-a-label-v1"
    ] = "c0.rdf.nfc-idna2003-strict-a-label-v1"
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


class RdfNamedGraph(RdfContractModel):
    graph_id: RequiredText
    graph_iri: RequiredText
    graph_role: RdfGraphRole
    required: bool
    contains_schema_triples: bool
    contains_instance_or_evidence_triples: bool
    expected_graph_hash: Sha256
    expected_triple_count: NonNegativeInt
    canonical_id_set_hash: Sha256
    canonical_id_count: NonNegativeInt
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


class RdfClassDefinition(RdfContractModel):
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


class RdfPropertyDefinition(RdfContractModel):
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


class RdfRelationshipDefinition(RdfContractModel):
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


class RdfVocabularyInventory(RdfContractModel):
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


class RdfExternalAlignment(RdfContractModel):
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


class RdfProjectionManifest(RdfContractModel):
    identity: CanonicalIdentityEnvelope
    rdf_projection_manifest_id: RequiredText
    source_authority: RdfSourceAuthorityTuple
    authority_reference_set_hash: Sha256
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
        if (
            self.authority_reference_set_hash
            != self.source_authority.reference_set_hash()
        ):
            raise ValueError(
                "authority_reference_set_hash does not match exact source references"
            )
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
        vocabulary_ids = sorted(
            [
                item.canonical_class_id
                for item in self.vocabulary.class_definitions
            ]
            + [
                item.canonical_property_id
                for item in self.vocabulary.property_definitions
            ]
            + [
                item.canonical_relationship_id
                for item in self.vocabulary.relationship_definitions
            ]
        )
        expected_graph_commitments = {
            "common_schema": (canonical_sha256(()), 0),
            "domain_schema": (
                canonical_sha256(vocabulary_ids),
                len(vocabulary_ids),
            ),
            "shacl_shapes": (
                canonical_sha256(vocabulary_ids),
                len(vocabulary_ids),
            ),
            "instances": (
                self.source_authority.instance_canonical_id_set_hash,
                self.source_authority.instance_canonical_id_count,
            ),
            "provenance_authority": (
                self.source_authority.provenance_canonical_id_set_hash,
                self.source_authority.provenance_canonical_id_count,
            ),
        }
        for graph in self.named_graphs:
            expected_commitment = expected_graph_commitments[graph.graph_role]
            if (
                graph.canonical_id_set_hash,
                graph.canonical_id_count,
            ) != expected_commitment:
                raise ValueError(
                    "graph canonical ID commitment differs from manifest authority"
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

    def canonical_id_binding_hash(self, exposure: RdfExposure) -> str:
        bindings = tuple(
            {
                "graph_id": graph.graph_id,
                "graph_role": graph.graph_role,
                "canonical_id_set_hash": graph.canonical_id_set_hash,
                "canonical_id_count": graph.canonical_id_count,
            }
            for graph in self.named_graphs
            if (
                exposure == "public_schema"
                and graph.graph_role in _PUBLIC_GRAPH_ROLES
            )
            or (
                exposure == "protected_dataset"
                and graph.graph_role in _PROTECTED_GRAPH_ROLES
            )
        )
        return canonical_sha256(bindings)


class RdfSerializedGraphBinding(RdfContractModel):
    graph_id: RequiredText
    graph_iri: RequiredText
    graph_role: RdfGraphRole
    required: bool
    triple_count: NonNegativeInt
    graph_hash: Sha256
    canonical_id_set_hash: Sha256
    canonical_id_count: NonNegativeInt
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


class RdfSerializationArtifact(RdfContractModel):
    identity: CanonicalIdentityEnvelope
    rdf_serialization_artifact_id: RequiredText
    rdf_projection_manifest_id: RequiredText
    rdf_projection_manifest_hash: Sha256
    authority_reference_set_hash: Sha256
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
    canonical_id_binding_hash: Sha256
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
        expected_id_binding_hash = canonical_sha256(
            tuple(
                {
                    "graph_id": item.graph_id,
                    "graph_role": item.graph_role,
                    "canonical_id_set_hash": item.canonical_id_set_hash,
                    "canonical_id_count": item.canonical_id_count,
                }
                for item in self.graph_bindings
            )
        )
        if self.canonical_id_binding_hash != expected_id_binding_hash:
            raise ValueError(
                "canonical_id_binding_hash does not match graph commitments"
            )
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
            mandatory_roles = {"provenance_authority"}
            if not mandatory_roles.issubset(roles) or not roles.issubset(
                _PROTECTED_GRAPH_ROLES
            ):
                raise ValueError(
                    "protected artifacts require provenance and optional instances only"
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
        if self.authority_reference_set_hash != manifest.authority_reference_set_hash:
            raise ValueError("artifact authority reference set hash mismatch")
        if (
            self.canonical_id_binding_hash
            != manifest.canonical_id_binding_hash(self.exposure)
        ):
            raise ValueError("artifact canonical ID binding differs from manifest")
        if self.serialization_format not in manifest.required_serialization_formats:
            raise ValueError("serialization format is not declared by the manifest")
        expected_graphs = tuple(
            graph
            for graph in manifest.named_graphs
            if (
                self.exposure == "public_schema"
                and graph.graph_role in _PUBLIC_GRAPH_ROLES
            )
            or (
                self.exposure == "protected_dataset"
                and graph.graph_role in _PROTECTED_GRAPH_ROLES
            )
        )
        actual = tuple(
            (
                item.graph_id,
                item.graph_iri,
                item.graph_role,
                item.required,
                item.access_policy_id,
                item.access_policy_hash,
                item.graph_hash,
                item.triple_count,
                item.canonical_id_set_hash,
                item.canonical_id_count,
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
                item.expected_graph_hash,
                item.expected_triple_count,
                item.canonical_id_set_hash,
                item.canonical_id_count,
            )
            for item in expected_graphs
        )
        if actual != expected:
            raise ValueError(
                "artifact graph IDs, roles, requirements, and policies must exactly "
                "match manifest inventory"
            )


class RdfSerializationObservation(RdfContractModel):
    rdf_serialization_artifact_id: RequiredText
    rdf_serialization_artifact_hash: Sha256
    serialization_format: RdfFormat
    exposure: RdfExposure
    media_type: RequiredText
    content_hash: Sha256
    canonical_dataset_hash: Sha256
    named_graph_ids: tuple[str, ...]
    graph_inventory_hash: Sha256
    canonical_id_binding_hash: Sha256
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


class RdfShaclValidationSummary(RdfContractModel):
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


class RdfValidationReceipt(RdfContractModel):
    identity: CanonicalIdentityEnvelope
    rdf_validation_receipt_id: RequiredText
    rdf_projection_manifest_id: RequiredText
    rdf_projection_manifest_hash: Sha256
    source_authority_hash: Sha256
    authority_reference_set_hash: Sha256
    canonical_id_partition_binding_hash: Sha256
    canonical_n_quads_artifact_ids: tuple[str, ...]
    canonical_dataset_hash_algorithm: Literal["RDFC-1.0"] = "RDFC-1.0"
    public_schema_canonical_dataset_hash: Sha256
    protected_dataset_canonical_dataset_hash: Sha256
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

    @field_validator("canonical_n_quads_artifact_ids", mode="before")
    @classmethod
    def _canonical_artifact_ids(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(
                value,
                field_name="canonical_n_quads_artifact_ids",
            )
        return value

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
        partitions = {
            (item.serialization_format, item.exposure)
            for item in self.observations
        }
        if len(partitions) != len(self.observations):
            raise ValueError(
                "round-trip observations must have unique format/exposure partitions"
            )
        formats = {item.serialization_format for item in self.observations}
        if formats != set(self.required_serialization_formats):
            raise ValueError(
                "observation formats must exactly equal required serialization formats"
            )
        if "canonical_n_quads" not in formats:
            raise ValueError("round-trip receipt requires canonical N-Quads")
        if any(
            item.authority_reference_set_hash
            != self.authority_reference_set_hash
            for item in self.observations
        ):
            raise ValueError(
                "observations must equal the receipt authority reference set hash"
            )
        expected_partitions = {
            (rdf_format, exposure)
            for rdf_format in self.required_serialization_formats
            for exposure in ("public_schema", "protected_dataset")
        }
        if partitions != expected_partitions:
            raise ValueError(
                "observations must cover public and protected partitions per format"
            )
        expected_partition_hash = canonical_sha256(
            tuple(
                {
                    "rdf_serialization_artifact_id": item.rdf_serialization_artifact_id,
                    "serialization_format": item.serialization_format,
                    "exposure": item.exposure,
                    "canonical_id_binding_hash": item.canonical_id_binding_hash,
                }
                for item in self.observations
            )
        )
        if (
            self.canonical_id_partition_binding_hash
            != expected_partition_hash
        ):
            raise ValueError(
                "canonical_id_partition_binding_hash does not match observations"
            )
        canonicals = tuple(
            item
            for item in self.observations
            if item.serialization_format == "canonical_n_quads"
        )
        if tuple(
            item.rdf_serialization_artifact_id for item in canonicals
        ) != self.canonical_n_quads_artifact_ids:
            raise ValueError("canonical N-Quads artifact IDs mismatch")
        canonical_by_exposure = {item.exposure: item for item in canonicals}
        expected_dataset_hashes = {
            "public_schema": self.public_schema_canonical_dataset_hash,
            "protected_dataset": self.protected_dataset_canonical_dataset_hash,
        }
        if self.public_schema_canonical_dataset_hash == (
            self.protected_dataset_canonical_dataset_hash
        ):
            raise ValueError("public and protected partition dataset hashes must differ")
        if {
            exposure: item.canonical_dataset_hash
            for exposure, item in canonical_by_exposure.items()
        } != expected_dataset_hashes:
            raise ValueError(
                "canonical N-Quads hashes must equal receipt partition dataset hashes"
            )
        failures = any(
            item.canonical_dataset_hash != expected_dataset_hashes[item.exposure]
            or item.named_graph_ids
            != canonical_by_exposure[item.exposure].named_graph_ids
            or item.triple_count
            != canonical_by_exposure[item.exposure].triple_count
            or item.missing_triple_count != 0
            or item.extra_triple_count != 0
            or item.authority_reference_set_hash
            != canonical_by_exposure[item.exposure].authority_reference_set_hash
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
        if self.authority_reference_set_hash != manifest.authority_reference_set_hash:
            raise ValueError("receipt authority reference set hash mismatch")
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
        artifact_partitions = {
            (item.serialization_format, item.exposure)
            for item in artifacts
        }
        expected_partitions = {
            (rdf_format, exposure)
            for rdf_format in manifest.required_serialization_formats
            for exposure in ("public_schema", "protected_dataset")
        }
        if artifact_partitions != expected_partitions:
            raise ValueError(
                "artifact set must contain public and protected partitions per format"
            )
        required_graph_ids = {
            graph.graph_id for graph in manifest.named_graphs if graph.required
        }
        for rdf_format in manifest.required_serialization_formats:
            partitions = {
                item.exposure: item
                for item in artifacts
                if item.serialization_format == rdf_format
            }
            public_ids = set(partitions["public_schema"].named_graph_ids)
            protected_ids = set(partitions["protected_dataset"].named_graph_ids)
            if public_ids.intersection(protected_ids):
                raise ValueError("public and protected graph partitions must be disjoint")
            if public_ids.union(protected_ids) != required_graph_ids:
                raise ValueError(
                    "artifact graph partition union must equal required manifest graphs"
                )
        for observation in self.observations:
            artifact = artifact_by_id[observation.rdf_serialization_artifact_id]
            artifact.validate_against_manifest(manifest)
            checks = (
                (
                    "artifact hash",
                    observation.rdf_serialization_artifact_hash,
                    artifact.serialization_artifact_hash,
                ),
                (
                    "authority reference set hash",
                    observation.authority_reference_set_hash,
                    artifact.authority_reference_set_hash,
                ),
                ("format", observation.serialization_format, artifact.serialization_format),
                ("exposure", observation.exposure, artifact.exposure),
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
                (
                    "canonical ID binding hash",
                    observation.canonical_id_binding_hash,
                    artifact.canonical_id_binding_hash,
                ),
                ("named graph IDs", observation.named_graph_ids, artifact.named_graph_ids),
                ("triple count", observation.triple_count, artifact.triple_count),
            )
            for name, observed, sealed in checks:
                if observed != sealed:
                    raise ValueError(f"receipt observation {name} mismatch")
        shapes_graph = next(
            graph
            for graph in manifest.named_graphs
            if graph.graph_role == "shacl_shapes"
        )
        if self.shacl_validation.shapes_hash != shapes_graph.expected_graph_hash:
            raise ValueError("SHACL shapes hash differs from manifest shapes graph")


class RdfProjectionAcceptanceBundle(RdfContractModel):
    """Self-contained proof that a complete RDF projection is accepted."""

    identity: CanonicalIdentityEnvelope
    rdf_projection_acceptance_bundle_id: RequiredText
    authority_reference_set_hash: Sha256
    canonical_id_partition_binding_hash: Sha256
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
        if (
            self.authority_reference_set_hash
            != self.manifest.authority_reference_set_hash
        ):
            raise ValueError("bundle authority reference set hash mismatch")
        if (
            self.canonical_id_partition_binding_hash
            != self.validation_receipt.canonical_id_partition_binding_hash
        ):
            raise ValueError("bundle canonical ID partition binding hash mismatch")
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
