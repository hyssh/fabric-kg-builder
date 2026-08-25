"""Behavior-free C0.Publish contracts for projection and governed delivery proof."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import parse_qsl, urlparse

from pydantic import Field, field_validator, model_validator

from .base import (
    ContractModel,
    RequiredText,
    Sha256,
    canonical_sha256,
    reject_secret_text,
    sorted_unique,
    utc_timestamp,
)
from .identity import CanonicalIdentityEnvelope, ImmutableSourceLocator

NonNegativeInt = Annotated[int, Field(ge=0)]
OntologyBigInt = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]
ProjectionKind = Literal[
    "parquet",
    "semantic_model",
    "ontology",
    "graph",
    "search",
]
AssetKind = Literal["original", "visual", "table", "derived", "other"]
AccessOperation = Literal["metadata", "content", "short_lived_url"]


def _secret_free(value: str, *, field_name: str) -> str:
    reject_secret_text(value, field_name=field_name)
    forbidden_query_keys = {
        "sig",
        "se",
        "sp",
        "sv",
        "spr",
        "st",
        "token",
        "access_token",
        "client_secret",
        "accountkey",
    }
    parsed = urlparse(value)
    if forbidden_query_keys.intersection(
        key.casefold() for key, _ in parse_qsl(parsed.query)
    ):
        raise ValueError(f"{field_name} must not contain a signed URL")
    return value


def _reject_secrets_in(value: Any, *, path: str = "contract") -> None:
    if isinstance(value, str):
        _secret_free(value, field_name=path)
        return
    if isinstance(value, ContractModel):
        for name, item in value.__dict__.items():
            _reject_secrets_in(item, path=f"{path}.{name}")
        return
    if isinstance(value, Mapping):
        for name, item in value.items():
            _reject_secrets_in(name, path=f"{path}.key")
            _reject_secrets_in(item, path=f"{path}.{name}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secrets_in(item, path=f"{path}[{index}]")


def _sorted_text(value: object, *, field_name: str) -> object:
    if isinstance(value, (list, tuple)):
        return sorted_unique(value, field_name=field_name)
    return value


def _require_unique(
    values: list[tuple[str, str]],
    *,
    namespace: str,
) -> None:
    owners: dict[str, str] = {}
    for physical_id, canonical_id in values:
        prior = owners.setdefault(physical_id, canonical_id)
        if prior != canonical_id:
            raise ValueError(
                f"{namespace} physical ID {physical_id!r} is reused by "
                f"{prior!r} and {canonical_id!r}"
            )


class PublicationAuthorityReferences(ContractModel):
    """Exact L3 membership and source-artifact authority references."""

    required_member_manifest_id: RequiredText
    required_member_manifest_contract_version: Literal["1.1.0"] = "1.1.0"
    required_member_manifest_schema_hash: Sha256
    required_member_manifest_hash: Sha256
    authoritative_collection_hash: Sha256
    source_artifact_manifest_id: RequiredText
    source_artifact_manifest_hash: Sha256

    def validate_required_member_manifest(
        self,
        manifest: Any,
        *,
        schema_hash: str,
    ) -> None:
        """Prove exact reference equality without deriving membership."""
        if manifest.identity.contract_kind != "c0.required_member_manifest":
            raise ValueError("referenced artifact is not a required member manifest")
        if (
            manifest.identity.contract_version
            != self.required_member_manifest_contract_version
        ):
            raise ValueError("required member manifest contract version mismatch")
        if schema_hash != self.required_member_manifest_schema_hash:
            raise ValueError("required member manifest schema hash mismatch")
        if manifest.required_member_manifest_id != self.required_member_manifest_id:
            raise ValueError("required member manifest ID mismatch")
        if manifest.manifest_hash != self.required_member_manifest_hash:
            raise ValueError("required member manifest hash mismatch")
        if (
            manifest.authoritative_collection_hash
            != self.authoritative_collection_hash
        ):
            raise ValueError("authoritative collection hash mismatch")

    def validate_source_artifact_manifest(self, manifest: Any) -> None:
        if manifest.artifact_manifest_id != self.source_artifact_manifest_id:
            raise ValueError("source artifact manifest ID mismatch")
        if manifest.manifest_hash != self.source_artifact_manifest_hash:
            raise ValueError("source artifact manifest hash mismatch")


class PropertyProjectionMapping(ContractModel):
    """One canonical property projected into physical publication namespaces."""

    canonical_property_id: RequiredText
    physical_column_id: RequiredText
    ontology_bigint_id: OntologyBigInt
    graph_property: RequiredText
    search_index_field: RequiredText
    search_filter_field: RequiredText | None = None
    search_vector_field: RequiredText | None = None
    data_agent_selected_property_id: RequiredText | None = None

    @field_validator(
        "canonical_property_id",
        "physical_column_id",
        "graph_property",
        "search_index_field",
        "search_filter_field",
        "search_vector_field",
        "data_agent_selected_property_id",
    )
    @classmethod
    def _reject_secrets(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _secret_free(value, field_name=info.field_name)


class SemanticTypeProjectionMapping(ContractModel):
    """Canonical type, hierarchy, identity-key, and field publication mapping."""

    canonical_semantic_type_id: RequiredText
    canonical_parent_semantic_type_id: RequiredText | None = None
    physical_table_id: RequiredText
    ontology_bigint_id: OntologyBigInt
    graph_label: RequiredText
    graph_aliases: tuple[str, ...] = ()
    canonical_instance_key_property_ids: tuple[str, ...]
    property_mappings: tuple[PropertyProjectionMapping, ...]

    @field_validator("graph_aliases", "canonical_instance_key_property_ids", mode="before")
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        return _sorted_text(value, field_name=info.field_name)

    @field_validator(
        "canonical_semantic_type_id",
        "canonical_parent_semantic_type_id",
        "physical_table_id",
        "graph_label",
    )
    @classmethod
    def _reject_secrets(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _secret_free(value, field_name=info.field_name)

    @field_validator("property_mappings", mode="before")
    @classmethod
    def _sort_properties(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.canonical_property_id
                        if isinstance(item, PropertyProjectionMapping)
                        else str(item.get("canonical_property_id", ""))
                    ),
                )
            )
        return value

    @model_validator(mode="after")
    def _keys_resolve(self) -> "SemanticTypeProjectionMapping":
        property_ids = [item.canonical_property_id for item in self.property_mappings]
        if len(set(property_ids)) != len(property_ids):
            raise ValueError("canonical property mappings must be unique within a type")
        if not self.canonical_instance_key_property_ids:
            raise ValueError("each semantic type requires canonical instance key fields")
        if not set(self.canonical_instance_key_property_ids).issubset(property_ids):
            raise ValueError(
                "canonical instance key fields must resolve to property mappings"
            )
        return self


class EndpointKeyProjectionMapping(ContractModel):
    canonical_property_id: RequiredText
    physical_column_id: RequiredText

    @field_validator("canonical_property_id", "physical_column_id")
    @classmethod
    def _reject_secrets(cls, value: str, info: Any) -> str:
        return _secret_free(value, field_name=info.field_name)


class RelationshipProjectionMapping(ContractModel):
    """Canonical relationship and exact endpoint key-field projection."""

    canonical_semantic_relationship_id: RequiredText
    source_semantic_type_id: RequiredText
    target_semantic_type_id: RequiredText
    physical_table_id: RequiredText
    ontology_bigint_id: OntologyBigInt
    graph_label: RequiredText
    graph_aliases: tuple[str, ...] = ()
    source_key_fields: tuple[EndpointKeyProjectionMapping, ...]
    target_key_fields: tuple[EndpointKeyProjectionMapping, ...]
    search_index_field: RequiredText | None = None

    @field_validator("graph_aliases", mode="before")
    @classmethod
    def _aliases(cls, value: object) -> object:
        return _sorted_text(value, field_name="graph_aliases")

    @field_validator(
        "canonical_semantic_relationship_id",
        "source_semantic_type_id",
        "target_semantic_type_id",
        "physical_table_id",
        "graph_label",
        "search_index_field",
    )
    @classmethod
    def _reject_secrets(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _secret_free(value, field_name=info.field_name)

    @field_validator("source_key_fields", "target_key_fields", mode="before")
    @classmethod
    def _sort_keys(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.canonical_property_id
                        if isinstance(item, EndpointKeyProjectionMapping)
                        else str(item.get("canonical_property_id", ""))
                    ),
                )
            )
        return value

    @model_validator(mode="after")
    def _endpoint_keys(self) -> "RelationshipProjectionMapping":
        for field_name, fields in (
            ("source_key_fields", self.source_key_fields),
            ("target_key_fields", self.target_key_fields),
        ):
            if not fields:
                raise ValueError(f"{field_name} must not be empty")
            canonical_ids = [item.canonical_property_id for item in fields]
            physical_ids = [item.physical_column_id for item in fields]
            if len(set(canonical_ids)) != len(canonical_ids):
                raise ValueError(f"{field_name} canonical IDs must be unique")
            if len(set(physical_ids)) != len(physical_ids):
                raise ValueError(f"{field_name} physical IDs must be unique")
        return self


class PublicationCrosswalk(ContractModel):
    """Canonical-to-physical mapping proof; never a membership authority."""

    identity: CanonicalIdentityEnvelope
    publication_crosswalk_id: RequiredText
    authority: PublicationAuthorityReferences
    semantic_contract_hash: Sha256
    stable_id_lock_id: RequiredText
    stable_id_lock_hash: Sha256
    hierarchy_hash: Sha256
    identity_policy_hash: Sha256
    source_projection_id: RequiredText
    source_projection_hash: Sha256
    semantic_type_mappings: tuple[SemanticTypeProjectionMapping, ...]
    relationship_mappings: tuple[RelationshipProjectionMapping, ...]
    crosswalk_hash: Sha256

    @field_validator("semantic_type_mappings", mode="before")
    @classmethod
    def _sort_types(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.canonical_semantic_type_id
                        if isinstance(item, SemanticTypeProjectionMapping)
                        else str(item.get("canonical_semantic_type_id", ""))
                    ),
                )
            )
        return value

    @field_validator("relationship_mappings", mode="before")
    @classmethod
    def _sort_relationships(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.canonical_semantic_relationship_id
                        if isinstance(item, RelationshipProjectionMapping)
                        else str(item.get("canonical_semantic_relationship_id", ""))
                    ),
                )
            )
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "PublicationCrosswalk":
        _reject_secrets_in(self)
        if self.identity.contract_kind != "c0.publication_crosswalk":
            raise ValueError("invalid publication crosswalk identity contract_kind")
        if self.identity.semantic_contract_hash != self.semantic_contract_hash:
            raise ValueError("semantic contract hash differs from identity authority")

        type_ids = [item.canonical_semantic_type_id for item in self.semantic_type_mappings]
        if len(set(type_ids)) != len(type_ids):
            raise ValueError("canonical semantic type mappings must be unique")
        relationship_ids = [
            item.canonical_semantic_relationship_id
            for item in self.relationship_mappings
        ]
        if len(set(relationship_ids)) != len(relationship_ids):
            raise ValueError("canonical relationship mappings must be unique")

        known_types = set(type_ids)
        all_properties: list[tuple[SemanticTypeProjectionMapping, PropertyProjectionMapping]] = []
        for type_mapping in self.semantic_type_mappings:
            parent = type_mapping.canonical_parent_semantic_type_id
            if parent is not None and parent not in known_types:
                raise ValueError("canonical parent semantic type does not resolve")
            all_properties.extend(
                (type_mapping, property_mapping)
                for property_mapping in type_mapping.property_mappings
            )
        canonical_property_ids = [
            mapping.canonical_property_id for _, mapping in all_properties
        ]
        if len(set(canonical_property_ids)) != len(canonical_property_ids):
            raise ValueError("canonical property mappings must be globally unique")

        type_by_id = {
            item.canonical_semantic_type_id: item
            for item in self.semantic_type_mappings
        }
        for relationship in self.relationship_mappings:
            if relationship.source_semantic_type_id not in type_by_id:
                raise ValueError("relationship source semantic type does not resolve")
            if relationship.target_semantic_type_id not in type_by_id:
                raise ValueError("relationship target semantic type does not resolve")
            source_keys = set(
                type_by_id[
                    relationship.source_semantic_type_id
                ].canonical_instance_key_property_ids
            )
            target_keys = set(
                type_by_id[
                    relationship.target_semantic_type_id
                ].canonical_instance_key_property_ids
            )
            if {item.canonical_property_id for item in relationship.source_key_fields} != source_keys:
                raise ValueError(
                    "relationship source key fields must equal canonical instance keys"
                )
            if {item.canonical_property_id for item in relationship.target_key_fields} != target_keys:
                raise ValueError(
                    "relationship target key fields must equal canonical instance keys"
                )

        _require_unique(
            [
                (item.physical_table_id, item.canonical_semantic_type_id)
                for item in self.semantic_type_mappings
            ]
            + [
                (item.physical_table_id, item.canonical_semantic_relationship_id)
                for item in self.relationship_mappings
            ],
            namespace="physical table",
        )
        _require_unique(
            [
                (str(item.ontology_bigint_id), item.canonical_semantic_type_id)
                for item in self.semantic_type_mappings
            ]
            + [
                (str(mapping.ontology_bigint_id), mapping.canonical_property_id)
                for _, mapping in all_properties
            ]
            + [
                (str(item.ontology_bigint_id), item.canonical_semantic_relationship_id)
                for item in self.relationship_mappings
            ],
            namespace="ontology BigInt",
        )
        _require_unique(
            [
                (label, item.canonical_semantic_type_id)
                for item in self.semantic_type_mappings
                for label in (item.graph_label, *item.graph_aliases)
            ]
            + [
                (label, item.canonical_semantic_relationship_id)
                for item in self.relationship_mappings
                for label in (item.graph_label, *item.graph_aliases)
            ],
            namespace="graph label or alias",
        )
        _require_unique(
            [
                (
                    mapping.physical_column_id,
                    f"{type_mapping.canonical_semantic_type_id}:"
                    f"{mapping.canonical_property_id}",
                )
                for type_mapping, mapping in all_properties
            ]
            + [
                (
                    field.physical_column_id,
                    f"{relationship.canonical_semantic_relationship_id}:source:"
                    f"{field.canonical_property_id}",
                )
                for relationship in self.relationship_mappings
                for field in relationship.source_key_fields
            ]
            + [
                (
                    field.physical_column_id,
                    f"{relationship.canonical_semantic_relationship_id}:target:"
                    f"{field.canonical_property_id}",
                )
                for relationship in self.relationship_mappings
                for field in relationship.target_key_fields
            ],
            namespace="physical column",
        )
        _require_unique(
            [
                (mapping.graph_property, mapping.canonical_property_id)
                for _, mapping in all_properties
            ],
            namespace="graph property",
        )
        _require_unique(
            [
                (physical_id, mapping.canonical_property_id)
                for _, mapping in all_properties
                for physical_id in (
                    mapping.search_index_field,
                    mapping.search_filter_field,
                    mapping.search_vector_field,
                )
                if physical_id is not None
            ]
            + [
                (item.search_index_field, item.canonical_semantic_relationship_id)
                for item in self.relationship_mappings
                if item.search_index_field is not None
            ],
            namespace="search field",
        )
        _require_unique(
            [
                (
                    mapping.data_agent_selected_property_id,
                    mapping.canonical_property_id,
                )
                for _, mapping in all_properties
                if mapping.data_agent_selected_property_id is not None
            ],
            namespace="Data Agent selected property",
        )

        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"crosswalk_hash"})
        )
        if self.crosswalk_hash != expected:
            raise ValueError("crosswalk_hash does not match publication crosswalk")
        return self

    def validate_upstream_authority(
        self,
        *,
        hierarchy_hash: str,
        identity_policy_hash: str,
        stable_id_lock_hash: str,
        source_projection_hash: str,
    ) -> None:
        checks = (
            ("hierarchy", self.hierarchy_hash, hierarchy_hash),
            ("identity policy", self.identity_policy_hash, identity_policy_hash),
            ("stable ID lock", self.stable_id_lock_hash, stable_id_lock_hash),
            ("source projection", self.source_projection_hash, source_projection_hash),
        )
        for name, sealed, authoritative in checks:
            if sealed != authoritative:
                raise ValueError(f"stale {name} hash")


class ProjectionEvidence(ContractModel):
    """One local observation used by a projection equivalence proof."""

    count: NonNegativeInt
    canonical_id_set_hash: Sha256
    row_fingerprint: Sha256 | None = None
    definition_fingerprint: Sha256 | None = None
    index_fingerprint: Sha256 | None = None


class ProjectionEquivalence(ContractModel):
    """Proof schema only; it performs no compilation, deployment, or read-back."""

    identity: CanonicalIdentityEnvelope
    projection_equivalence_id: RequiredText
    authority: PublicationAuthorityReferences
    publication_crosswalk_id: RequiredText
    publication_crosswalk_hash: Sha256
    source_projection_id: RequiredText
    source_projection_hash: Sha256
    projection_kind: ProjectionKind
    expected: ProjectionEvidence
    compiled: ProjectionEvidence
    deployed: ProjectionEvidence
    read_back: ProjectionEvidence
    missing_canonical_ids: tuple[str, ...] = ()
    extra_canonical_ids: tuple[str, ...] = ()
    equivalent: bool
    equivalence_hash: Sha256

    @field_validator("missing_canonical_ids", "extra_canonical_ids", mode="before")
    @classmethod
    def _id_sets(cls, value: object, info: Any) -> object:
        return _sorted_text(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "ProjectionEquivalence":
        _reject_secrets_in(self)
        if self.identity.contract_kind != "c0.projection_equivalence":
            raise ValueError("invalid projection equivalence identity contract_kind")
        overlap = set(self.missing_canonical_ids).intersection(
            self.extra_canonical_ids
        )
        if overlap:
            raise ValueError("missing and extra canonical IDs must be disjoint")

        fingerprint_field = {
            "parquet": "row_fingerprint",
            "semantic_model": "definition_fingerprint",
            "ontology": "definition_fingerprint",
            "graph": "definition_fingerprint",
            "search": "index_fingerprint",
        }[self.projection_kind]
        snapshots = (self.expected, self.compiled, self.deployed, self.read_back)
        if any(getattr(snapshot, fingerprint_field) is None for snapshot in snapshots):
            raise ValueError(
                f"{self.projection_kind} equivalence requires {fingerprint_field}"
            )
        if self.equivalent:
            if self.missing_canonical_ids or self.extra_canonical_ids:
                raise ValueError("equivalent proof cannot contain missing or extra IDs")
            first = snapshots[0].model_dump(mode="json")
            if any(snapshot.model_dump(mode="json") != first for snapshot in snapshots[1:]):
                raise ValueError(
                    "equivalent proof requires exact count, ID-set, and fingerprint equality"
                )

        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"equivalence_hash"})
        )
        if self.equivalence_hash != expected_hash:
            raise ValueError("equivalence_hash does not match projection proof")
        return self

    def validate_crosswalk(self, crosswalk: PublicationCrosswalk) -> None:
        if crosswalk.publication_crosswalk_id != self.publication_crosswalk_id:
            raise ValueError("publication crosswalk ID mismatch")
        if crosswalk.crosswalk_hash != self.publication_crosswalk_hash:
            raise ValueError("publication crosswalk hash mismatch")
        if crosswalk.authority != self.authority:
            raise ValueError("publication authority references differ")
        if crosswalk.source_projection_id != self.source_projection_id:
            raise ValueError("source projection ID mismatch")
        if crosswalk.source_projection_hash != self.source_projection_hash:
            raise ValueError("source projection hash mismatch")


class PrincipalScope(ContractModel):
    principal_type: Literal[
        "user",
        "group",
        "service_principal",
        "managed_identity",
        "application",
    ]
    principal_id: RequiredText
    resource_scope_ids: tuple[str, ...]

    @field_validator("resource_scope_ids", mode="before")
    @classmethod
    def _scopes(cls, value: object) -> object:
        return _sorted_text(value, field_name="resource_scope_ids")

    @field_validator("principal_id")
    @classmethod
    def _principal_secret_free(cls, value: str) -> str:
        return _secret_free(value, field_name="principal_id")

    @model_validator(mode="after")
    def _scope_required(self) -> "PrincipalScope":
        if not self.resource_scope_ids:
            raise ValueError("principal scope requires at least one resource scope")
        for item in self.resource_scope_ids:
            _secret_free(item, field_name="resource_scope_ids")
        return self


class AccessPolicy(ContractModel):
    """Deterministic authorization metadata with no credentials or signed URLs."""

    identity: CanonicalIdentityEnvelope
    access_policy_id: RequiredText
    principal_scopes: tuple[PrincipalScope, ...]
    allowed_operations: tuple[AccessOperation, ...]
    sensitivity: Literal["public", "internal", "confidential", "restricted"]
    retention_class: RequiredText
    retain_until_utc: datetime | None = None
    legal_hold: bool
    legal_hold_reference: RequiredText | None = None
    authorization_resource_id: RequiredText
    policy_hash: Sha256

    @field_validator("retain_until_utc", mode="before")
    @classmethod
    def _parse_utc(cls, value: object) -> object:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value is None:
            return None
        return utc_timestamp(value)

    @field_validator("allowed_operations", mode="before")
    @classmethod
    def _operations(cls, value: object) -> object:
        return _sorted_text(value, field_name="allowed_operations")

    @field_validator("principal_scopes", mode="before")
    @classmethod
    def _sort_principals(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.principal_type,
                        item.principal_id,
                    )
                    if isinstance(item, PrincipalScope)
                    else (
                        str(item.get("principal_type", "")),
                        str(item.get("principal_id", "")),
                    ),
                )
            )
        return value

    @field_validator(
        "access_policy_id",
        "retention_class",
        "legal_hold_reference",
        "authorization_resource_id",
    )
    @classmethod
    def _reject_secrets(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _secret_free(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "AccessPolicy":
        _reject_secrets_in(self)
        if self.identity.contract_kind != "c0.access_policy":
            raise ValueError("invalid access policy identity contract_kind")
        if not self.principal_scopes:
            raise ValueError("access policy requires at least one principal scope")
        principals = [
            (item.principal_type, item.principal_id)
            for item in self.principal_scopes
        ]
        if len(set(principals)) != len(principals):
            raise ValueError("principal scopes must be unique")
        if not self.allowed_operations:
            raise ValueError("access policy requires at least one allowed operation")
        if self.legal_hold != (self.legal_hold_reference is not None):
            raise ValueError(
                "legal_hold and legal_hold_reference must be present together"
            )
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"policy_hash"})
        )
        if self.policy_hash != expected:
            raise ValueError("policy_hash does not match access policy")
        return self


class StorageReference(ContractModel):
    """Credential-free immutable storage coordinates, never an access URL."""

    storage_kind: Literal[
        "azure_blob",
        "onelake",
        "sharepoint",
        "object_store",
        "other",
    ]
    storage_account_resource_id: RequiredText
    container_id: RequiredText
    object_id: RequiredText
    object_version_id: RequiredText
    storage_reference_hash: Sha256

    @field_validator(
        "storage_account_resource_id",
        "container_id",
        "object_id",
        "object_version_id",
    )
    @classmethod
    def _reject_secrets(cls, value: str, info: Any) -> str:
        return _secret_free(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "StorageReference":
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"storage_reference_hash"})
        )
        if self.storage_reference_hash != expected:
            raise ValueError("storage_reference_hash does not match storage coordinates")
        return self


class GovernedAssetReference(ContractModel):
    """Generic immutable delivery asset; text citations remain independent."""

    identity: CanonicalIdentityEnvelope
    governed_asset_reference_id: RequiredText
    asset_kind: AssetKind
    source_file_id: RequiredText
    asset_id: RequiredText
    asset_version_id: RequiredText
    immutable_locator: ImmutableSourceLocator
    content_hash: Sha256
    storage_reference: StorageReference
    access_policy_id: RequiredText
    access_policy_hash: Sha256
    on_demand_url_policy: Literal["not_permitted", "authorized_short_lived"]
    asset_reference_hash: Sha256

    @field_validator(
        "governed_asset_reference_id",
        "source_file_id",
        "asset_id",
        "asset_version_id",
        "access_policy_id",
    )
    @classmethod
    def _reject_secrets(cls, value: str, info: Any) -> str:
        return _secret_free(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "GovernedAssetReference":
        _reject_secrets_in(self)
        if self.identity.contract_kind != "c0.governed_asset_reference":
            raise ValueError("invalid governed asset identity contract_kind")
        identity_values = (
            ("source_file_id", self.identity.source_file_id, self.source_file_id),
            ("asset_id", self.identity.asset_id, self.asset_id),
            ("asset_version_id", self.identity.asset_version_id, self.asset_version_id),
            ("content_hash", self.identity.content_hash, self.content_hash),
            (
                "immutable_locator",
                self.identity.immutable_locator,
                self.immutable_locator,
            ),
        )
        for name, identity_value, asset_value in identity_values:
            if identity_value != asset_value:
                raise ValueError(f"{name} differs from identity authority")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"asset_reference_hash"})
        )
        if self.asset_reference_hash != expected:
            raise ValueError("asset_reference_hash does not match governed asset")
        return self

    def validate_access_policy(self, policy: AccessPolicy) -> None:
        if policy.access_policy_id != self.access_policy_id:
            raise ValueError("access policy ID mismatch")
        if policy.policy_hash != self.access_policy_hash:
            raise ValueError("access policy hash mismatch")
        if (
            self.on_demand_url_policy == "authorized_short_lived"
            and "short_lived_url" not in policy.allowed_operations
        ):
            raise ValueError("access policy does not allow short-lived URL issuance")
