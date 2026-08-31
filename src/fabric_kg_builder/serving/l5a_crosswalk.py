"""Deterministic L5a publication crosswalk compiler.

L5a requires a :class:`PublicationCrosswalkV1_2` that exactly covers the sealed
L4 authority: one type mapping per non-tombstoned entity type, one ownership
mapping per declared property, and one relationship mapping per declared
relationship type. This module derives that crosswalk from the sealed source
alone, so the same L4 projection always compiles to the same crosswalk hash.

Stable identifier assignment is positional over lexicographically sorted
canonical IDs, which makes every physical name, ontology BigInt, and graph
label a pure function of the sealed domain contract.

A relationship type may declare several admissible source or target types. The
crosswalk carries a single *physical representative* per endpoint, and L5a's
emitted definitions publish the full declared and compatible endpoint sets
alongside it (``source_semantic_type_ids``,
``compatible_source_semantic_type_ids``), with the representative's key columns
labelled ``physical_projection_only``/``schema_only``. Choosing the
lexicographically smallest admissible endpoint is therefore a deterministic
naming decision, not a narrowing of the published semantics.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow.parquet as pq

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.contracts.identity import (
    CanonicalIdentityEnvelope,
    ImmutableSourceLocator,
)
from fabric_kg_builder.contracts.publication import (
    AccessPolicy,
    GovernedAssetReference,
    StorageReference,
    EndpointPhysicalKeyBindingV1_1,
    InheritedPropertyReferenceV1_1,
    PhysicalPropertyBindingV1_1,
    PrincipalScope,
    PublicationAuthorityReferencesV1_2,
    PublicationCrosswalkIdentityV1_2,
    PublicationCrosswalkV1_2,
    RelationshipProjectionMappingV1_1,
    SemanticPropertyOwnershipMappingV1_1,
    SemanticTypeProjectionMappingV1_1,
)
from fabric_kg_builder.contracts.extraction import RequiredMemberManifestV1_1
from fabric_kg_builder.domain.models import DomainContractV2
from fabric_kg_builder.serving.structured_publication import (
    build_l5a_governed_assets,
)

__all__ = [
    "L5aCrosswalkError",
    "compile_access_policy",
    "compile_publication_crosswalk",
]

_TYPE_BIGINT_BASE = 1_000_000
_PROPERTY_BIGINT_BASE = 2_000_000
_RELATIONSHIP_BIGINT_BASE = 3_000_000
_BIGINT_NAMESPACE_CAPACITY = 1_000_000

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


class L5aCrosswalkError(RuntimeError):
    """Raised when a sealed L4 source cannot compile to an exact crosswalk."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _slug(value: str) -> str:
    slug = _SLUG_PATTERN.sub("_", value.lower()).strip("_")
    return slug or "x"


def _unique_slugs(canonical_ids: Sequence[str], *, prefix: str) -> dict[str, str]:
    """Map each canonical ID to a unique physical slug.

    Collisions are broken by the canonical ID's own digest rather than by
    position, so adding a new type never renames an existing one.
    """

    assigned: dict[str, str] = {}
    taken: set[str] = set()
    for canonical_id in sorted(canonical_ids):
        candidate = f"{prefix}{_slug(canonical_id)}"
        if candidate in taken:
            candidate = f"{candidate}_{canonical_sha256(canonical_id)[:8]}"
        if candidate in taken:  # pragma: no cover - digest collision
            raise L5aCrosswalkError(
                "L5A_CROSSWALK_NAME_COLLISION",
                f"cannot assign a unique physical name for {canonical_id}",
            )
        assigned[canonical_id] = candidate
        taken.add(candidate)
    return assigned


def _bigints(canonical_ids: Sequence[str], *, base: int) -> dict[str, int]:
    ordered = sorted(canonical_ids)
    if len(ordered) > _BIGINT_NAMESPACE_CAPACITY:
        raise L5aCrosswalkError(
            "L5A_CROSSWALK_BIGINT_EXHAUSTED",
            f"more than {_BIGINT_NAMESPACE_CAPACITY} canonical IDs in one namespace",
        )
    return {
        canonical_id: base + ordinal
        for ordinal, canonical_id in enumerate(ordered, start=1)
    }


def _authority_row(source: Any) -> Mapping[str, Any]:
    rows = pq.read_table(
        source.resolve("semantic_publication_authority")
    ).to_pylist()
    if len(rows) != 1:
        raise L5aCrosswalkError(
            "L5A_CROSSWALK_AUTHORITY_MISSING",
            "sealed L4 source must carry exactly one publication authority row",
        )
    return rows[0]


def _identity_values(source: Any, contract_kind: str) -> dict[str, Any]:
    """Rebase the sealed L4 receipt identity onto a publication contract kind."""

    values = source.receipt.identity.model_dump(mode="python")
    values["contract_kind"] = contract_kind
    return values


def _authority_references(source: Any) -> PublicationAuthorityReferencesV1_2:
    """Anchor the crosswalk to every required-member manifest L3 sealed.

    A domain contract that declares no ``structured_fact_set`` completeness
    requirement seals no manifest, in which case the source artifact manifest is
    the only admissible anchor and exactly one unanchored crosswalk stands in
    for the empty cover.
    """

    manifest_rows = pq.read_table(
        source.resolve("semantic_required_member_manifests")
    ).to_pylist()
    if not manifest_rows:
        return PublicationAuthorityReferencesV1_2(
            source_artifact_manifest_id=source.input_manifest.artifact_manifest_id,
            source_artifact_manifest_hash=source.input_manifest.manifest_hash,
        )
    if len(manifest_rows) != 1:
        raise L5aCrosswalkError(
            "L5A_CROSSWALK_MULTIPLE_MANIFESTS",
            "compiling more than one required-member manifest needs one crosswalk "
            "per manifest, which this compiler does not yet emit",
        )
    manifest = manifest_rows[0]
    manifest_id = str(manifest["required_member_manifest_id"])
    entry = next(
        (
            item for item in source.input_manifest.entries
            if item.artifact_id == manifest_id
        ),
        None,
    )
    if entry is None:
        raise L5aCrosswalkError(
            "L5A_CROSSWALK_MANIFEST_UNANCHORED",
            f"sealed manifest {manifest_id} is absent from the L4 input manifest",
        )
    expected_schema_hash = canonical_sha256(
        RequiredMemberManifestV1_1.model_json_schema()
    )
    if entry.schema_hash != expected_schema_hash:
        raise L5aCrosswalkError(
            "L5A_CROSSWALK_MANIFEST_SCHEMA_DRIFT",
            f"manifest {manifest_id} was sealed against a different schema",
        )
    return PublicationAuthorityReferencesV1_2(
        required_member_manifest_id=manifest_id,
        required_member_manifest_contract_version="1.1.0",
        required_member_manifest_schema_hash=entry.schema_hash,
        required_member_manifest_hash=str(manifest["manifest_hash"]),
        authoritative_collection_hash=str(manifest["authoritative_collection_hash"]),
        source_artifact_manifest_id=source.input_manifest.artifact_manifest_id,
        source_artifact_manifest_hash=source.input_manifest.manifest_hash,
    )


def compile_publication_crosswalk(
    source: Any,
    *,
    publication_crosswalk_id: str = "publication-crosswalk:l5a",
    stable_id_lock_id: str = "stable-id-lock:l5a",
) -> PublicationCrosswalkV1_2:
    """Compile the exact crosswalk a sealed L4 source admits."""

    authority_row = _authority_row(source)
    contract = DomainContractV2.model_validate_json(
        authority_row["domain_contract_json"]
    )
    entity_types = [
        item for item in contract.candidate_model.entity_types
        if not item.tombstoned
    ]
    if not entity_types:
        raise L5aCrosswalkError(
            "L5A_CROSSWALK_NO_TYPES",
            "sealed domain contract declares no publishable entity type",
        )
    entity_by_id = {item.type_id: item for item in entity_types}
    relationship_types = list(contract.candidate_model.relationship_types)

    property_owner: dict[str, tuple[str, Any]] = {}
    for definition in entity_types:
        for prop in definition.declared_properties:
            if prop.property_id in property_owner:
                raise L5aCrosswalkError(
                    "L5A_CROSSWALK_PROPERTY_AMBIGUOUS",
                    f"property {prop.property_id} is declared by more than one type",
                )
            property_owner[prop.property_id] = (definition.type_id, prop)

    type_slugs = _unique_slugs(list(entity_by_id), prefix="l5a_type_")
    relationship_slugs = _unique_slugs(
        [item.relationship_type_id for item in relationship_types],
        prefix="l5a_rel_",
    )
    property_slugs = _unique_slugs(list(property_owner), prefix="l5a_prop_")
    type_bigints = _bigints(list(entity_by_id), base=_TYPE_BIGINT_BASE)
    property_bigints = _bigints(list(property_owner), base=_PROPERTY_BIGINT_BASE)
    relationship_bigints = _bigints(
        [item.relationship_type_id for item in relationship_types],
        base=_RELATIONSHIP_BIGINT_BASE,
    )

    ownership_mappings = tuple(
        SemanticPropertyOwnershipMappingV1_1(
            canonical_property_id=property_id,
            owner_semantic_type_id=property_owner[property_id][0],
            data_type=property_owner[property_id][1].value_type,
            value_semantics_id=f"value-semantics:{property_id}",
            ontology_bigint_id=property_bigints[property_id],
            graph_property=property_slugs[property_id],
            data_agent_selected_property_id=None,
        )
        for property_id in sorted(property_owner)
    )

    effective_by_type = contract.hierarchy_closure.effective_property_ids_by_type
    type_mappings = []
    for type_id in sorted(entity_by_id):
        definition = entity_by_id[type_id]
        effective_ids = tuple(effective_by_type[type_id])
        if not effective_ids:
            raise L5aCrosswalkError(
                "L5A_CROSSWALK_TYPE_WITHOUT_PROPERTIES",
                f"type {type_id} has no effective property to materialize",
            )
        local_ids = tuple(
            prop.property_id for prop in definition.declared_properties
        )
        inherited_ids = tuple(
            property_id for property_id in effective_ids
            if property_id not in local_ids
        )
        root = entity_by_id.get(definition.identity_root_type_id)
        if root is None:
            raise L5aCrosswalkError(
                "L5A_CROSSWALK_IDENTITY_ROOT_MISSING",
                f"identity root {definition.identity_root_type_id} for {type_id} "
                "is not a publishable type",
            )
        type_mappings.append(SemanticTypeProjectionMappingV1_1(
            canonical_semantic_type_id=type_id,
            canonical_parent_semantic_type_id=definition.parent_type_id,
            physical_table_id=type_slugs[type_id],
            ontology_bigint_id=type_bigints[type_id],
            graph_label=type_slugs[type_id].upper(),
            graph_aliases=(),
            locally_owned_canonical_property_ids=local_ids,
            inherited_property_references=tuple(
                InheritedPropertyReferenceV1_1(
                    canonical_property_id=property_id,
                    owner_semantic_type_id=property_owner[property_id][0],
                    data_type=property_owner[property_id][1].value_type,
                    value_semantics_id=f"value-semantics:{property_id}",
                )
                for property_id in inherited_ids
            ),
            canonical_instance_key_property_ids=tuple(
                root.identity_key_policy.business_key_fields
            ),
            physical_property_bindings=tuple(
                PhysicalPropertyBindingV1_1(
                    canonical_property_id=property_id,
                    owner_semantic_type_id=property_owner[property_id][0],
                    data_type=property_owner[property_id][1].value_type,
                    value_semantics_id=f"value-semantics:{property_id}",
                    physical_column_id=property_slugs[property_id],
                    search_index_field=property_slugs[property_id],
                    search_filter_field=property_slugs[property_id],
                    search_vector_field=None,
                )
                for property_id in sorted(effective_ids)
            ),
            physical_surrogate_key_bindings=(),
        ))
    key_ids_by_type = {
        item.canonical_semantic_type_id: item.canonical_instance_key_property_ids
        for item in type_mappings
    }

    relationship_mappings = []
    for definition in sorted(
        relationship_types,
        key=lambda item: item.relationship_type_id,
    ):
        relationship_id = definition.relationship_type_id
        source_type = _representative(
            definition.source_type_ids,
            key_ids_by_type,
            relationship_id=relationship_id,
            endpoint="source",
        )
        target_type = _representative(
            definition.target_type_ids,
            key_ids_by_type,
            relationship_id=relationship_id,
            endpoint="target",
        )
        slug = relationship_slugs[relationship_id]
        relationship_mappings.append(RelationshipProjectionMappingV1_1(
            canonical_semantic_relationship_id=relationship_id,
            source_semantic_type_id=source_type,
            target_semantic_type_id=target_type,
            physical_table_id=slug,
            ontology_bigint_id=relationship_bigints[relationship_id],
            graph_label=slug.upper(),
            graph_aliases=(),
            source_canonical_key_property_ids=key_ids_by_type[source_type],
            target_canonical_key_property_ids=key_ids_by_type[target_type],
            source_key_bindings=tuple(
                EndpointPhysicalKeyBindingV1_1(
                    canonical_property_id=property_id,
                    physical_column_id=f"{slug}__src__{property_slugs[property_id]}",
                )
                for property_id in sorted(key_ids_by_type[source_type])
            ),
            target_key_bindings=tuple(
                EndpointPhysicalKeyBindingV1_1(
                    canonical_property_id=property_id,
                    physical_column_id=f"{slug}__tgt__{property_slugs[property_id]}",
                )
                for property_id in sorted(key_ids_by_type[target_type])
            ),
            search_index_field=None,
        ))

    stable_id_lock = {
        "types": {key: type_bigints[key] for key in sorted(type_bigints)},
        "properties": {
            key: property_bigints[key] for key in sorted(property_bigints)
        },
        "relationships": {
            key: relationship_bigints[key] for key in sorted(relationship_bigints)
        },
        "physical_names": {
            **{key: type_slugs[key] for key in sorted(type_slugs)},
            **{key: property_slugs[key] for key in sorted(property_slugs)},
            **{key: relationship_slugs[key] for key in sorted(relationship_slugs)},
        },
    }

    identity_values = _identity_values(source, "c0.publication_crosswalk")
    identity_values["contract_version"] = "1.2.0"
    values = {
        "identity": PublicationCrosswalkIdentityV1_2.model_validate(identity_values),
        "publication_crosswalk_id": publication_crosswalk_id,
        "authority": _authority_references(source),
        "semantic_contract_hash": source.projection.sealed_semantic_contract_hash,
        "stable_id_lock_id": stable_id_lock_id,
        "stable_id_lock_hash": canonical_sha256(stable_id_lock),
        "hierarchy_hash": str(authority_row["hierarchy_hash"]),
        "identity_policy_hash": str(authority_row["identity_policy_hash"]),
        "source_projection_id": source.projection.projection_id,
        "source_projection_hash": source.projection.projection_hash,
        "semantic_property_ownership_mappings": ownership_mappings,
        "semantic_type_mappings": tuple(type_mappings),
        "relationship_mappings": tuple(relationship_mappings),
    }
    return PublicationCrosswalkV1_2(
        **values,
        crosswalk_hash=canonical_sha256(values),
    )


def _representative(
    candidate_type_ids: Sequence[str],
    key_ids_by_type: Mapping[str, tuple[str, ...]],
    *,
    relationship_id: str,
    endpoint: str,
) -> str:
    """Pick the deterministic physical representative for a relationship endpoint.

    L5a publishes the full declared and compatible endpoint sets from the sealed
    contract, so this choice only names the schema-only key columns.
    """

    admissible = sorted(
        item for item in candidate_type_ids if item in key_ids_by_type
    )
    if not admissible:
        raise L5aCrosswalkError(
            "L5A_CROSSWALK_ENDPOINT_UNRESOLVED",
            f"relationship {relationship_id} has no publishable {endpoint} type",
        )
    return admissible[0]


def compile_access_policy(
    source: Any,
    *,
    access_policy_id: str,
    principal_id: str,
    resource_scope_id: str,
    authorization_resource_id: str,
    sensitivity: str = "internal",
    retention_class: str = "retention:project",
) -> AccessPolicy:
    """Compile the release-owned access policy for a sealed L4 source."""

    values = {
        "identity": CanonicalIdentityEnvelope.model_validate(
            _identity_values(source, "c0.access_policy")
        ),
        "access_policy_id": access_policy_id,
        "principal_scopes": (
            PrincipalScope(
                principal_type="managed_identity",
                principal_id=principal_id,
                resource_scope_ids=(resource_scope_id,),
            ),
        ),
        "allowed_operations": ("content", "metadata"),
        "sensitivity": sensitivity,
        "retention_class": retention_class,
        "retain_until_utc": None,
        "legal_hold": False,
        "legal_hold_reference": None,
        "authorization_resource_id": authorization_resource_id,
    }
    return AccessPolicy(**values, policy_hash=canonical_sha256(values))


_FABRIC_COLLECTION = {
    "semantic_model": "semanticModels",
    "ontology": "ontologies",
    "graph": "graphModels",
}


def _target_name(target_id: str) -> str:
    """Release-owned item name carried by a target id."""

    return target_id.split(":", 1)[1] if ":" in target_id else target_id


def _target_uri(kind: str, *, workspace_id: str, target_name: str) -> str:
    """Immutable address of one published L5a target."""

    if kind == "parquet":
        return (
            f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/"
            f"{target_name}.Lakehouse/Tables"
        )
    return (
        "https://api.fabric.microsoft.com/v1/workspaces/"
        f"{workspace_id}/{_FABRIC_COLLECTION[kind]}/{target_name}"
    )


def compile_governed_assets(
    source: Any,
    *,
    crosswalks: Sequence[PublicationCrosswalkV1_2],
    access_policy: AccessPolicy,
    target_ids: Mapping[str, str],
    workspace_id: str,
    definition_version_id: str = "definition-version:1",
) -> tuple[GovernedAssetReference, ...]:
    """Compile the governed asset reference set for the four L5a targets.

    Targets are addressed by their release-owned name inside the Fabric
    workspace rather than by an item GUID, because a GUID is only known after
    a live create. Dry-run and live therefore seal an identical asset set.
    """

    workspace_resource_id = f"resource:fabric-workspace:{workspace_id}"

    storage_references: dict[str, StorageReference] = {}
    locators: dict[str, ImmutableSourceLocator] = {}
    for kind in sorted(target_ids):
        target_id = target_ids[kind]
        storage_values = {
            "storage_kind": "onelake" if kind == "parquet" else "other",
            "storage_account_resource_id": workspace_resource_id,
            "container_id": f"container:{kind}",
            "object_id": target_id,
            "object_version_id": definition_version_id,
        }
        storage_references[kind] = StorageReference(
            **storage_values,
            storage_reference_hash=canonical_sha256(storage_values),
        )
        locator_values: dict[str, Any] = {
            "locator_version": "1.0",
            "source_uri": _target_uri(
                kind,
                workspace_id=workspace_id,
                target_name=_target_name(target_id),
            ),
            "blob_uri": None,
            "blob_version_id": None,
            "page": None,
            "slide": None,
            "sheet": None,
            "cell_range": None,
            "section_path": None,
            "char_start": None,
            "char_end": None,
            "polygon": None,
            "coordinate_system": None,
            "transform": None,
            "native_object_id": None,
            "native_layer_id": None,
            "tile_id": None,
            "sheet_zone": None,
        }
        locators[kind] = ImmutableSourceLocator(
            **locator_values,
            locator_hash=canonical_sha256(locator_values),
        )
    return build_l5a_governed_assets(
        source,
        crosswalks=tuple(crosswalks),
        access_policy=access_policy,
        target_ids=dict(target_ids),
        storage_references=storage_references,
        immutable_locators=locators,
    )
