from __future__ import annotations

import dataclasses
import copy
import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.contracts.identity import (
    CanonicalIdentityEnvelope,
    ImmutableSourceLocator,
)
from fabric_kg_builder.contracts.publication import (
    AccessPolicy,
    EndpointPhysicalKeyBindingV1_1,
    GovernedAssetReference,
    InheritedPropertyReferenceV1_1,
    PhysicalPropertyBindingV1_1,
    PrincipalScope,
    PublicationAuthorityReferences,
    PublicationCrosswalkIdentityV1_1,
    PublicationCrosswalkV1_1,
    RelationshipProjectionMappingV1_1,
    SemanticPropertyOwnershipMappingV1_1,
    SemanticTypeProjectionMappingV1_1,
    StorageReference,
)
from fabric_kg_builder.contracts.receipts import StageReceipt
from fabric_kg_builder.contracts.resources import StageResourceMetrics
from fabric_kg_builder.domain.models import DomainContractV2
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from fabric_kg_builder.semantic.source_tables import require_l5_publication_receipt
from fabric_kg_builder.serving.structured_publication import (
    L5A_MAX_FABRIC_CALLS,
    L5A_TARGET_ORDER,
    L5aPublishOperation,
    L5aPublicationError,
    L5aRemoteAccounting,
    L5aStateOperation,
    L5aTableSnapshot,
    L5aTargetState,
    build_l5a_governed_assets,
    compile_l5a_publication,
    require_l5a_publication_receipt,
    run_l5a,
    _CallAccounting,
    _equivalences,
    _expected_state,
    _required_member_snapshots,
)
from tests.unit.test_schema2_projection_stage import _l3_with_sealed_manifest
from tests.unit.test_schema2_validation_stage import _subtypes


class _FakeClient:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], L5aTargetState] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_publish: str | None = None
        self.raise_after_create: str | None = None
        self.tamper_read_back: str | None = None
        self.raise_read_back: str | None = None
        self.tamper_required_members: str | None = None
        self.remote_sequence = 0

    def _accounting(self, verb, target_kind, *, errors=()):
        self.remote_sequence += 1
        return L5aRemoteAccounting(
            operation_refs=(
                f"remote:{self.remote_sequence}:{verb}:{target_kind}",
            ),
            request_bytes=11,
            response_bytes=7,
            retry_count=1 if verb == "publish" else 0,
            retry_wait_ms=2 if verb == "publish" else 0,
            error_codes=tuple(errors),
        )

    def inspect(self, target_kind, target_id):
        self.calls.append(("inspect", target_kind))
        return L5aStateOperation(
            state=self.states.get((target_kind, target_id)),
            accounting=self._accounting("inspect", target_kind),
        )

    def publish(
        self,
        target_kind,
        target_id,
        *,
        definition_path,
        table_paths,
        access_policy,
        expected_state,
        publication_token,
    ):
        self.calls.append(("publish", target_kind))
        if self.fail_publish == target_kind:
            return L5aPublishOperation(
                target_kind=target_kind,
                target_id=target_id,
                created=False,
                publication_token=publication_token,
                applied=False,
                accounting=self._accounting(
                    "publish",
                    target_kind,
                    errors=("REMOTE_PUBLISH_FAILED",),
                ),
            )
        assert definition_path.is_file()
        assert table_paths and all(path.is_file() for path in table_paths.values())
        definition = json.loads(definition_path.read_text("utf-8"))
        snapshots = tuple(
            L5aTableSnapshot(**item) for item in definition["tables"]
        )
        required_member_manifest_rows = tuple(
            dict(row)
            for row in pq.read_table(
                table_paths["l4_semantic_required_member_manifests"]
            ).to_pylist()
        )
        required_member_rows = tuple(
            dict(row)
            for row in pq.read_table(
                table_paths["l4_semantic_required_members"]
            ).to_pylist()
        )
        key = (target_kind, target_id)
        if self.states.get(key) != expected_state:
            raise RuntimeError(f"concurrent {target_kind} state change")
        created = key not in self.states
        self.states[key] = L5aTargetState(
            target_kind=target_kind,
            target_id=target_id,
            target_version=definition["publication_version"],
            definition=definition,
            table_snapshots=snapshots,
            access_policy_id=access_policy.access_policy_id,
            access_policy_hash=access_policy.policy_hash,
            publication_token=publication_token,
            required_member_manifest_rows=required_member_manifest_rows,
            required_member_rows=required_member_rows,
        )
        if self.raise_after_create == target_kind:
            raise RuntimeError(f"response lost after {target_kind} creation")
        return L5aPublishOperation(
            target_kind=target_kind,
            target_id=target_id,
            created=created,
            publication_token=publication_token,
            applied=True,
            accounting=self._accounting("publish", target_kind),
        )

    def read_back(self, target_kind, target_id):
        self.calls.append(("read_back", target_kind))
        if self.raise_read_back == target_kind:
            self.raise_read_back = None
            raise RuntimeError(f"forced {target_kind} read-back failure")
        state = self.states.get((target_kind, target_id))
        if state is not None and self.tamper_read_back == target_kind:
            definition = dict(state.definition)
            definition["source_projection_hash"] = "f" * 64
            state = dataclasses.replace(state, definition=definition)
        if state is not None and self.tamper_required_members == target_kind:
            changed_rows = [dict(row) for row in state.required_member_rows]
            changed_rows[0]["member_canonical_id"] = "entity:wrong"
            state = dataclasses.replace(
                state,
                required_member_rows=tuple(changed_rows),
            )
        return L5aStateOperation(
            state=state,
            accounting=self._accounting("read-back", target_kind),
        )

    def cleanup(self, target_kind, target_id, *, publication_token):
        self.calls.append(("cleanup", target_kind))
        state = self.states.get((target_kind, target_id))
        applied = state is None or state.publication_token == publication_token
        if state is not None and applied:
            self.states.pop((target_kind, target_id), None)
        return L5aPublishOperation(
            target_kind=target_kind,
            target_id=target_id,
            created=False,
            publication_token=publication_token,
            applied=applied,
            accounting=self._accounting("cleanup", target_kind),
        )

    def restore(
        self,
        target_kind,
        target_id,
        *,
        prior_state,
        publication_token,
    ):
        self.calls.append(("restore", target_kind))
        state = self.states.get((target_kind, target_id))
        applied = state is not None and state.publication_token == publication_token
        if applied:
            self.states[(target_kind, target_id)] = prior_state
        return L5aPublishOperation(
            target_kind=target_kind,
            target_id=target_id,
            created=False,
            publication_token=publication_token,
            applied=applied,
            accounting=self._accounting("restore", target_kind),
        )


def _identity(source, contract_kind: str) -> CanonicalIdentityEnvelope:
    values = source.receipt.identity.model_dump(mode="python")
    values["contract_kind"] = contract_kind
    return CanonicalIdentityEnvelope.model_validate(values)


def _policy(source) -> AccessPolicy:
    values = {
        "identity": _identity(source, "c0.access_policy"),
        "access_policy_id": "access-policy:l5a",
        "principal_scopes": (
            PrincipalScope(
                principal_type="managed_identity",
                principal_id="principal:fabric-publisher",
                resource_scope_ids=("resource:l5a",),
            ),
        ),
        "allowed_operations": ("content", "metadata"),
        "sensitivity": "internal",
        "retention_class": "retention:project",
        "retain_until_utc": None,
        "legal_hold": False,
        "legal_hold_reference": None,
        "authorization_resource_id": "authorization-resource:l5a",
    }
    return AccessPolicy(**values, policy_hash=canonical_sha256(values))


def _assets(source, crosswalk, policy, target_ids):
    storage_references = {}
    locators = {}
    for kind, target_id in target_ids.items():
        storage_values = {
            "storage_kind": "onelake" if kind == "parquet" else "other",
            "storage_account_resource_id": "resource:fabric-workspace",
            "container_id": f"container:{kind}",
            "object_id": target_id,
            "object_version_id": "definition-version:1",
        }
        storage_references[kind] = StorageReference(
            **storage_values,
            storage_reference_hash=canonical_sha256(storage_values),
        )
        locator_values = {
            "locator_version": "1.0",
            "source_uri": f"https://example.invalid/l5a/{kind}?version=1",
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
        crosswalks=(crosswalk,),
        access_policy=policy,
        target_ids=target_ids,
        storage_references=storage_references,
        immutable_locators=locators,
    )


def _crosswalk(source) -> PublicationCrosswalkV1_1:
    manifest = pq.read_table(
        source.resolve("semantic_required_member_manifests")
    ).to_pylist()[0]
    authority_row = pq.read_table(
        source.resolve("semantic_publication_authority")
    ).to_pylist()[0]
    contract = DomainContractV2.model_validate_json(
        authority_row["domain_contract_json"]
    )
    ownership_mappings = []
    property_by_id = {}
    for definition in contract.candidate_model.entity_types:
        for prop in definition.declared_properties:
            property_by_id[prop.property_id] = (definition.type_id, prop)
            ownership_mappings.append(SemanticPropertyOwnershipMappingV1_1(
                canonical_property_id=prop.property_id,
                owner_semantic_type_id=definition.type_id,
                data_type=prop.value_type,
                value_semantics_id=f"value-semantics:{prop.property_id}",
                ontology_bigint_id=2001 + len(ownership_mappings),
                graph_property=(
                    f"graph_{prop.property_id.replace(':', '_')}"
                ),
                data_agent_selected_property_id=None,
            ))
    type_mappings = []
    for ordinal, definition in enumerate(
        contract.candidate_model.entity_types,
        start=1,
    ):
        type_id = definition.type_id
        suffix = type_id.rsplit(".", 1)[-1].replace("-", "_")
        effective_ids = contract.hierarchy_closure.effective_property_ids_by_type[
            type_id
        ]
        local_ids = tuple(prop.property_id for prop in definition.declared_properties)
        inherited_ids = tuple(
            property_id for property_id in effective_ids
            if property_id not in local_ids
        )
        inherited_refs = tuple(
            InheritedPropertyReferenceV1_1(
                canonical_property_id=property_id,
                owner_semantic_type_id=property_by_id[property_id][0],
                data_type=property_by_id[property_id][1].value_type,
                value_semantics_id=f"value-semantics:{property_id}",
            )
            for property_id in inherited_ids
        )
        physical_bindings = tuple(
            PhysicalPropertyBindingV1_1(
                canonical_property_id=property_id,
                owner_semantic_type_id=property_by_id[property_id][0],
                data_type=property_by_id[property_id][1].value_type,
                value_semantics_id=f"value-semantics:{property_id}",
                physical_column_id=f"{suffix}_{index}",
                search_index_field=f"{suffix}_{index}",
                search_filter_field=f"{suffix}_{index}",
                search_vector_field=None,
            )
            for index, property_id in enumerate(effective_ids, start=1)
        )
        assert physical_bindings
        root = next(
            item for item in contract.candidate_model.entity_types
            if item.type_id == definition.identity_root_type_id
        )
        type_mappings.append(SemanticTypeProjectionMappingV1_1(
            canonical_semantic_type_id=type_id,
            canonical_parent_semantic_type_id=definition.parent_type_id,
            physical_table_id=f"l5a_{suffix}",
            ontology_bigint_id=1000 + ordinal,
            graph_label=f"L5A_{suffix.upper()}",
            graph_aliases=(),
            locally_owned_canonical_property_ids=local_ids,
            inherited_property_references=inherited_refs,
            canonical_instance_key_property_ids=tuple(
                root.identity_key_policy.business_key_fields
            ),
            physical_property_bindings=physical_bindings,
            physical_surrogate_key_bindings=(),
        ))
    relationship_definition = contract.candidate_model.relationship_types[0]
    source_type = relationship_definition.source_type_ids[0]
    target_type = relationship_definition.target_type_ids[0]
    relationship_id = relationship_definition.relationship_type_id
    type_mapping_by_id = {
        item.canonical_semantic_type_id: item for item in type_mappings
    }
    relationship = RelationshipProjectionMappingV1_1(
        canonical_semantic_relationship_id=relationship_id,
        source_semantic_type_id=source_type,
        target_semantic_type_id=target_type,
        physical_table_id="l5a_membership",
        ontology_bigint_id=3001,
        graph_label="L5A_MEMBER",
        graph_aliases=(),
        source_canonical_key_property_ids=type_mapping_by_id[
            source_type
        ].canonical_instance_key_property_ids,
        target_canonical_key_property_ids=type_mapping_by_id[
            target_type
        ].canonical_instance_key_property_ids,
        source_key_bindings=tuple(
            EndpointPhysicalKeyBindingV1_1(
                canonical_property_id=property_id,
                physical_column_id=f"source_{index}",
            )
            for index, property_id in enumerate(
                type_mapping_by_id[
                    source_type
                ].canonical_instance_key_property_ids,
                start=1,
            )
        ),
        target_key_bindings=tuple(
            EndpointPhysicalKeyBindingV1_1(
                canonical_property_id=property_id,
                physical_column_id=f"target_{index}",
            )
            for index, property_id in enumerate(
                type_mapping_by_id[
                    target_type
                ].canonical_instance_key_property_ids,
                start=1,
            )
        ),
        search_index_field=None,
    )
    entry = next(
        item for item in source.input_manifest.entries
        if item.artifact_id == manifest["required_member_manifest_id"]
    )
    authority = PublicationAuthorityReferences(
        required_member_manifest_id=manifest["required_member_manifest_id"],
        required_member_manifest_contract_version="1.1.0",
        required_member_manifest_schema_hash=entry.schema_hash,
        required_member_manifest_hash=manifest["manifest_hash"],
        authoritative_collection_hash=manifest["authoritative_collection_hash"],
        source_artifact_manifest_id=source.input_manifest.artifact_manifest_id,
        source_artifact_manifest_hash=source.input_manifest.manifest_hash,
    )
    values = {
        "identity": PublicationCrosswalkIdentityV1_1.model_validate({
            **_identity(
                source,
                "c0.publication_crosswalk",
            ).model_dump(mode="python"),
            "contract_version": "1.1.0",
        }),
        "publication_crosswalk_id": "publication-crosswalk:l5a",
        "authority": authority,
        "semantic_contract_hash": source.projection.sealed_semantic_contract_hash,
        "stable_id_lock_id": "stable-id-lock:l5a",
        "stable_id_lock_hash": "b" * 64,
        "hierarchy_hash": authority_row["hierarchy_hash"],
        "identity_policy_hash": authority_row["identity_policy_hash"],
        "source_projection_id": source.projection.projection_id,
        "source_projection_hash": source.projection.projection_hash,
        "semantic_property_ownership_mappings": tuple(sorted(
            ownership_mappings,
            key=lambda item: item.canonical_property_id,
        )),
        "semantic_type_mappings": tuple(sorted(
            type_mappings,
            key=lambda item: item.canonical_semantic_type_id,
        )),
        "relationship_mappings": (relationship,),
    }
    return PublicationCrosswalkV1_1(
        **values,
        crosswalk_hash=canonical_sha256(values),
    )


def _inputs(
    tmp_path: Path,
    *,
    member_count: int = 1,
    extra_types=(),
    extra_type_properties=None,
    extra_relationship_targets=False,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    l4 = run_l4(
        _l3_with_sealed_manifest(
            tmp_path,
            member_count=member_count,
            type_properties={
                "semantic-type:manufacturing.record": ({
                    "property_id": "property:record:canonical-id",
                    "display_name": "Record ID",
                    "value_type": "string",
                    "required": True,
                },),
                "semantic-type:manufacturing.subject": ({
                    "property_id": "property:subject:canonical-id",
                    "display_name": "Subject ID",
                    "value_type": "string",
                    "required": True,
                },),
                **(extra_type_properties or {}),
            },
            extra_types=extra_types,
            extra_relationship_targets=extra_relationship_targets,
            identity_business_keys={
                "semantic-type:manufacturing.record": (
                    "property:record:canonical-id",
                ),
                "semantic-type:manufacturing.subject": (
                    "property:subject:canonical-id",
                ),
            },
            inject_identity_keys=True,
        ),
        state_root=tmp_path / ".fkg" / "l4",
    )
    source = l4.sealed_source()
    policy = _policy(source)
    crosswalk = _crosswalk(source)
    target_ids = {
        "parquet": "target:lakehouse",
        "semantic_model": "target:semantic-model",
        "ontology": "target:ontology",
        "graph": "target:graph",
    }
    return {
        "source": source,
        "crosswalks": (crosswalk,),
        "access_policy": policy,
        "governed_assets": _assets(
            source,
            crosswalk,
            policy,
            target_ids,
        ),
        "target_ids": target_ids,
    }


def _nontrivial_inputs(tmp_path: Path):
    extra_types = list(copy.deepcopy(_subtypes("manufacturing")))
    intermediate_id = "semantic-type:manufacturing.record-b"
    grandchild = extra_types[0]["proposed_type"]
    grandchild["parent_type_id"] = intermediate_id
    grandchild["identity_root_type_id"] = "semantic-type:manufacturing.record"
    return _inputs(
        tmp_path,
        extra_types=tuple(extra_types),
        extra_relationship_targets=True,
        extra_type_properties={
            "semantic-type:manufacturing.record": (
                {
                    "property_id": "property:record:canonical-id",
                    "display_name": "Record ID",
                    "value_type": "string",
                    "required": True,
                },
                {
                    "property_id": "property:record:status",
                    "display_name": "Record Status",
                    "value_type": "string",
                    "required": False,
                },
            ),
            "semantic-type:manufacturing.record-a": ({
                "property_id": "property:record-a:detail",
                "display_name": "Record A Detail",
                "value_type": "string",
                "required": False,
            },),
            "semantic-type:manufacturing.record-b": ({
                "property_id": "property:record-b:detail",
                "display_name": "Record B Detail",
                "value_type": "string",
                "required": False,
            },),
        },
    )


@pytest.mark.unit
def test_l5a_persists_and_reads_back_all_structured_targets(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    result = run_l5a(
        **inputs,
        client=client,
        state_root=tmp_path / ".fkg" / "l5a",
    )

    assert result.receipt.status == "succeeded"
    assert result.metrics.fabric_calls == 12
    assert result.metrics.network_request_bytes == 12 * 11
    assert result.metrics.network_response_bytes == 12 * 7
    assert result.metrics.retry_count == 4
    assert result.metrics.retry_wait_ms == 8
    assert len(result.receipt.remote_operation_refs) == 13
    assert result.metrics.search_calls == 0
    assert result.metrics.search_documents_written == 0
    assert {proof.projection_kind for proof in result.projection_equivalences} == set(
        L5A_TARGET_ORDER
    )
    assert all(proof.expected == proof.read_back for proof in result.projection_equivalences)
    assert all(proof.authority == inputs["crosswalks"][0].authority for proof in result.projection_equivalences)
    assert result.compiled.definitions["ontology"]["native_inheritance_assumed"] is False
    assert result.compiled.definitions["graph"]["edge_types"]
    for mapping in result.compiled.definitions["parquet"]["semantic_types"]:
        assert mapping["physical_identity_source"] == "stable_canonical_entity_id"
        assert mapping["physical_identity_column"] == "__canonical_id"
        table = result.compiled.tables[mapping["physical_table_id"]]
        for prop in mapping["properties"]:
            assert table[prop["physical_column_id"]].null_count == table.num_rows
    assert all(
        item["key_properties"] == ["__canonical_id"]
        for item in result.compiled.definitions["graph"]["node_types"]
    )
    assert not any("search" in kind for kind in result.compiled.definitions)
    require_l5a_publication_receipt(inputs["source"], result)
    require_l5_publication_receipt(inputs["source"], result)
    required = result.compiled.required_member_snapshots[0]
    for proof in result.projection_equivalences:
        assert proof.authority.required_member_manifest_id == (
            required.required_member_manifest_id
        )
        assert proof.expected.count == len(required.canonical_ids)
        assert proof.expected.canonical_id_set_hash == canonical_sha256(
            required.canonical_ids
        )
        if proof.projection_kind == "parquet":
            assert proof.expected.row_fingerprint == required.row_fingerprint


@pytest.mark.unit
def test_l5a_derives_inherited_properties_and_full_endpoint_sets(
    tmp_path: Path,
) -> None:
    inputs = _nontrivial_inputs(tmp_path)
    compiled = compile_l5a_publication(**inputs)
    contract = DomainContractV2.model_validate_json(
        pq.read_table(
            inputs["source"].resolve("semantic_publication_authority")
        ).to_pylist()[0]["domain_contract_json"]
    )
    child_id = "semantic-type:manufacturing.record-a"
    child = next(
        item
        for item in compiled.definitions["parquet"]["semantic_types"]
        if item["canonical_semantic_type_id"] == child_id
    )
    assert child["flattened_ancestor_type_ids"] == (
        contract.hierarchy_closure.ancestors_by_type[child_id]
    )
    assert child["flattened_ancestor_type_ids"] != [
        "semantic-type:manufacturing.record-b",
        "semantic-type:manufacturing.record",
    ]
    assert {
        item["canonical_property_id"] for item in child["properties"]
    } == set(
        contract.hierarchy_closure.effective_property_ids_by_type[child_id]
    )
    assert {
        item["declaring_semantic_type_id"] for item in child["properties"]
    } == {
        "semantic-type:manufacturing.record",
        "semantic-type:manufacturing.record-a",
        "semantic-type:manufacturing.record-b",
    }
    relationship = compiled.definitions["parquet"]["relationships"][0]
    authority = contract.candidate_model.relationship_types[0]
    assert relationship["source_semantic_type_ids"] == authority.source_type_ids
    assert relationship["target_semantic_type_ids"] == authority.target_type_ids
    assert len(relationship["source_semantic_type_ids"]) > 1
    assert len(relationship["target_semantic_type_ids"]) > 1
    ontology_relationship = compiled.definitions["ontology"][
        "relationship_types"
    ][0]
    graph_relationship = compiled.definitions["graph"]["edge_types"][0]
    for published in (ontology_relationship, graph_relationship):
        assert published["declared_source_semantic_type_ids"] == (
            authority.source_type_ids
        )
        assert published["declared_target_semantic_type_ids"] == (
            authority.target_type_ids
        )
        assert published["allowed_source_semantic_type_ids"] == (
            contract.hierarchy_closure
            .compatible_source_type_ids_by_relationship[
                authority.relationship_type_id
            ]
        )
        assert published["allowed_target_semantic_type_ids"] == (
            contract.hierarchy_closure
            .compatible_target_type_ids_by_relationship[
                authority.relationship_type_id
            ]
        )
    assert "source_entity_type_id" not in ontology_relationship
    assert "target_entity_type_id" not in ontology_relationship
    assert ontology_relationship[
        "physical_source_entity_type_representative_id"
    ]
    assert ontology_relationship[
        "physical_target_entity_type_representative_id"
    ]


@pytest.mark.unit
@pytest.mark.parametrize("mutation", ("omit", "extra"))
def test_l5a_rejects_nonexact_canonical_property_ownership(
    tmp_path: Path,
    mutation: str,
) -> None:
    inputs = _nontrivial_inputs(tmp_path)
    crosswalk = inputs["crosswalks"][0]
    values = crosswalk.model_dump(mode="python", exclude={"crosswalk_hash"})
    values["semantic_property_ownership_mappings"] = list(
        values["semantic_property_ownership_mappings"]
    )
    record = next(
        item
        for item in values["semantic_type_mappings"]
        if item["canonical_semantic_type_id"]
        == "semantic-type:manufacturing.record"
    )
    record["locally_owned_canonical_property_ids"] = list(
        record["locally_owned_canonical_property_ids"]
    )
    record["physical_property_bindings"] = list(
        record["physical_property_bindings"]
    )
    property_id = "property:record:status"
    if mutation == "omit":
        values["semantic_property_ownership_mappings"] = [
            item for item in values["semantic_property_ownership_mappings"]
            if item["canonical_property_id"] != property_id
        ]
        record["locally_owned_canonical_property_ids"] = [
            item for item in record["locally_owned_canonical_property_ids"]
            if item != property_id
        ]
        for mapping in values["semantic_type_mappings"]:
            mapping["inherited_property_references"] = [
                item for item in mapping["inherited_property_references"]
                if item["canonical_property_id"] != property_id
            ]
            mapping["physical_property_bindings"] = [
                item for item in mapping["physical_property_bindings"]
                if item["canonical_property_id"] != property_id
            ]
    else:
        forged_id = "property:forged:shadow"
        values["semantic_property_ownership_mappings"].append({
            "canonical_property_id": forged_id,
            "owner_semantic_type_id": record["canonical_semantic_type_id"],
            "data_type": "string",
            "value_semantics_id": f"value-semantics:{forged_id}",
            "ontology_bigint_id": 2999,
            "graph_property": "graph_property_forged_shadow",
            "data_agent_selected_property_id": None,
        })
        record["locally_owned_canonical_property_ids"].append(forged_id)
        record["physical_property_bindings"].append({
            "canonical_property_id": forged_id,
            "owner_semantic_type_id": record["canonical_semantic_type_id"],
            "data_type": "string",
            "value_semantics_id": f"value-semantics:{forged_id}",
            "physical_column_id": "forged_shadow",
            "search_index_field": "forged_shadow",
            "search_filter_field": None,
            "search_vector_field": None,
        })
    values["semantic_property_ownership_mappings"] = sorted(
        values["semantic_property_ownership_mappings"],
        key=lambda item: item["canonical_property_id"],
    )
    record["locally_owned_canonical_property_ids"] = sorted(
        record["locally_owned_canonical_property_ids"]
    )
    record["physical_property_bindings"] = sorted(
        record["physical_property_bindings"],
        key=lambda item: item["canonical_property_id"],
    )
    forged = PublicationCrosswalkV1_1(
        **values,
        crosswalk_hash=canonical_sha256(values),
    )

    with pytest.raises(
        L5aPublicationError,
        match="L5A_PROPERTY_MAPPING_SET_MISMATCH",
    ):
        compile_l5a_publication(
            **{**inputs, "crosswalks": (forged,)},
        )


@pytest.mark.unit
def test_crosswalk_rejects_duplicate_inherited_physical_property_owner(
    tmp_path: Path,
) -> None:
    inputs = _nontrivial_inputs(tmp_path)
    crosswalk = inputs["crosswalks"][0]
    values = crosswalk.model_dump(mode="python", exclude={"crosswalk_hash"})
    child = next(
        item
        for item in values["semantic_type_mappings"]
        if item["canonical_semantic_type_id"]
        == "semantic-type:manufacturing.record-a"
    )
    child["locally_owned_canonical_property_ids"] = [
        *child["locally_owned_canonical_property_ids"],
        "property:record:canonical-id",
    ]

    with pytest.raises(ValueError, match="both local and inherited"):
        PublicationCrosswalkV1_1(
            **values,
            crosswalk_hash=canonical_sha256(values),
        )


@pytest.mark.unit
def test_l5a_rejects_arbitrary_descendant_semantic_instance_key(
    tmp_path: Path,
) -> None:
    inputs = _nontrivial_inputs(tmp_path)
    crosswalk = inputs["crosswalks"][0]
    values = crosswalk.model_dump(mode="python", exclude={"crosswalk_hash"})
    child = next(
        item
        for item in values["semantic_type_mappings"]
        if item["canonical_semantic_type_id"]
        == "semantic-type:manufacturing.record-a"
    )
    child["canonical_instance_key_property_ids"] = [
        "property:record-a:detail"
    ]
    relationship = values["relationship_mappings"][0]
    assert relationship["target_semantic_type_id"] == child[
        "canonical_semantic_type_id"
    ]
    relationship["target_canonical_key_property_ids"] = [
        "property:record-a:detail"
    ]
    relationship["target_key_bindings"] = [{
        "canonical_property_id": "property:record-a:detail",
        "physical_column_id": "target_1",
    }]
    forged = PublicationCrosswalkV1_1(
        **values,
        crosswalk_hash=canonical_sha256(values),
    )

    with pytest.raises(
        L5aPublicationError,
        match="L5A_TYPE_AUTHORITY_MISMATCH",
    ):
        compile_l5a_publication(
            **{**inputs, "crosswalks": (forged,)},
        )


@pytest.mark.unit
def test_l5a_rejects_physical_relationship_representative_outside_authority(
    tmp_path: Path,
) -> None:
    inputs = _nontrivial_inputs(tmp_path)
    crosswalk = inputs["crosswalks"][0]
    values = crosswalk.model_dump(mode="python", exclude={"crosswalk_hash"})
    relationship = values["relationship_mappings"][0]
    subject = next(
        item
        for item in values["semantic_type_mappings"]
        if item["canonical_semantic_type_id"]
        == "semantic-type:manufacturing.subject"
    )
    relationship["source_semantic_type_id"] = (
        "semantic-type:manufacturing.subject"
    )
    relationship["source_canonical_key_property_ids"] = list(
        subject["canonical_instance_key_property_ids"]
    )
    relationship["source_key_bindings"] = [{
        "canonical_property_id": subject[
            "canonical_instance_key_property_ids"
        ][0],
        "physical_column_id": "source_id",
    }]
    forged = PublicationCrosswalkV1_1(
        **values,
        crosswalk_hash=canonical_sha256(values),
    )

    with pytest.raises(
        L5aPublicationError,
        match="L5A_RELATIONSHIP_AUTHORITY_MISMATCH",
    ):
        compile_l5a_publication(
            **{**inputs, "crosswalks": (forged,)},
        )


@pytest.mark.unit
def test_l5a_empty_and_nonempty_member_evidence_cannot_match(
    tmp_path: Path,
) -> None:
    result = run_l5a(
        **_inputs(tmp_path),
        client=_FakeClient(),
        state_root=tmp_path / ".fkg" / "l5a",
    )
    nonempty = result.compiled.required_member_snapshots[0]
    empty = dataclasses.replace(
        nonempty,
        canonical_ids=(nonempty.canonical_ids[0],),
        row_fingerprint=canonical_sha256({
            "manifest": nonempty.required_member_manifest_id,
            "members": [],
        }),
    )

    assert len(empty.canonical_ids) == 1
    assert canonical_sha256(empty.canonical_ids) != canonical_sha256(
        nonempty.canonical_ids
    )
    assert empty.row_fingerprint != nonempty.row_fingerprint


@pytest.mark.unit
def test_l5a_different_collections_have_unique_manifest_proofs(
    tmp_path: Path,
) -> None:
    first_inputs = _inputs(tmp_path / "one", member_count=1)
    second_inputs = _inputs(tmp_path / "two", member_count=2)
    first = run_l5a(
        **first_inputs,
        client=_FakeClient(),
        state_root=tmp_path / "one" / ".fkg" / "l5a",
    )
    second = run_l5a(
        **second_inputs,
        client=_FakeClient(),
        state_root=tmp_path / "two" / ".fkg" / "l5a",
    )

    assert first.projection_equivalences[0].expected != (
        second.projection_equivalences[0].expected
    )
    swapped = dataclasses.replace(
        first,
        projection_equivalences=second.projection_equivalences,
    )
    with pytest.raises(
        L5aPublicationError,
        match="L5A_PUBLICATION_RECEIPT_INVALID",
    ):
        require_l5a_publication_receipt(first_inputs["source"], swapped)


@pytest.mark.unit
def test_l5a_multi_manifest_proofs_are_unique_and_manifest_specific(
    tmp_path: Path,
) -> None:
    first = compile_l5a_publication(
        **_inputs(tmp_path / "one", member_count=1)
    )
    second = compile_l5a_publication(
        **_inputs(tmp_path / "two", member_count=2)
    )
    second_crosswalk_values = second.crosswalks[0].model_dump(
        mode="python",
        exclude={"crosswalk_hash"},
    )
    authority_values = dict(second_crosswalk_values["authority"])
    authority_values.update({
        "source_artifact_manifest_id": (
            first.source.input_manifest.artifact_manifest_id
        ),
        "source_artifact_manifest_hash": first.source.input_manifest.manifest_hash,
    })
    second_crosswalk_values["authority"] = PublicationAuthorityReferences(
        **authority_values
    )
    second_crosswalk_values["publication_crosswalk_id"] = (
        "publication-crosswalk:l5a:second"
    )
    second_crosswalk = PublicationCrosswalkV1_1(
        **second_crosswalk_values,
        crosswalk_hash=canonical_sha256(second_crosswalk_values),
    )
    second_snapshot = dataclasses.replace(
        second.required_member_snapshots[0],
        source_artifact_manifest_id=(
            first.source.input_manifest.artifact_manifest_id
        ),
        source_artifact_manifest_hash=first.source.input_manifest.manifest_hash,
    )
    combined = dataclasses.replace(
        first,
        crosswalks=(first.crosswalks[0], second_crosswalk),
        required_member_snapshots=(
            first.required_member_snapshots[0],
            second_snapshot,
        ),
        required_member_manifest_rows=(
            *first.required_member_manifest_rows,
            *second.required_member_manifest_rows,
        ),
        required_member_rows=(
            *first.required_member_rows,
            *second.required_member_rows,
        ),
    )
    states = {
        kind: _expected_state(
            combined,
            kind,
            publication_token="multi-manifest",
        )
        for kind in L5A_TARGET_ORDER
    }

    proofs = _equivalences(combined, states)

    assert len(proofs) == 2 * len(L5A_TARGET_ORDER)
    assert len({proof.projection_equivalence_id for proof in proofs}) == len(proofs)
    by_manifest = {}
    for proof in proofs:
        by_manifest.setdefault(
            proof.authority.required_member_manifest_id,
            set(),
        ).add((
            proof.expected.count,
            proof.expected.canonical_id_set_hash,
            proof.expected.row_fingerprint
            or proof.expected.definition_fingerprint,
        ))
    assert len(by_manifest) == 2
    assert len({next(iter(values)) for values in by_manifest.values()}) == 2

    graph_state = states["graph"]
    changed_rows = [dict(row) for row in graph_state.required_member_rows]
    changed_rows[0]["required_member_manifest_id"] = (
        second_snapshot.required_member_manifest_id
    )
    tampered_states = {
        **states,
        "graph": dataclasses.replace(
            graph_state,
            required_member_rows=tuple(changed_rows),
        ),
    }
    with pytest.raises(
        L5aPublicationError,
        match="L5A_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
    ):
        _equivalences(combined, tampered_states)


@pytest.mark.unit
def test_l5a_required_member_proof_uses_only_carried_l4_tables(
    tmp_path: Path,
) -> None:
    compiled = compile_l5a_publication(**_inputs(tmp_path))
    shadowed = {
        **compiled.tables,
        "semantic_required_member_manifests": compiled.tables[
            next(
                table_id
                for table_id in compiled.tables
                if table_id.startswith("l5a_")
            )
        ],
        "semantic_required_members": compiled.tables[
            "l4_semantic_asserted_entities"
        ],
    }

    assert _required_member_snapshots(compiled.source, shadowed) == (
        compiled.required_member_snapshots
    )


@pytest.mark.unit
def test_l5a_idempotent_reuse_reads_back_without_republishing(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    state_root = tmp_path / ".fkg" / "l5a"
    first = run_l5a(**inputs, client=client, state_root=state_root)
    client.calls.clear()

    second = run_l5a(**inputs, client=client, state_root=state_root)

    assert first.output_manifest == second.output_manifest
    assert second.reused
    assert second.receipt.status == "skipped"
    assert second.metrics.fabric_calls == 4
    assert second.metrics.network_request_bytes == 4 * 11
    assert second.metrics.network_response_bytes == 4 * 7
    assert len(second.receipt.remote_operation_refs) == 4
    assert client.calls == [
        ("read_back", "parquet"),
        ("read_back", "semantic_model"),
        ("read_back", "ontology"),
        ("read_back", "graph"),
    ]
    calls_before_readiness = list(client.calls)
    require_l5a_publication_receipt(inputs["source"], second)
    assert client.calls == calls_before_readiness


@pytest.mark.unit
def test_l5a_reuse_readiness_accepts_accounted_retries(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    class _RetryingReadbackClient(_FakeClient):
        def _accounting(self, verb, target_kind, *, errors=()):
            accounting = super()._accounting(
                verb,
                target_kind,
                errors=errors,
            )
            if verb == "read-back":
                return dataclasses.replace(
                    accounting,
                    retry_count=1,
                    retry_wait_ms=3,
                )
            return accounting

    client = _RetryingReadbackClient()
    state_root = tmp_path / ".fkg" / "l5a"
    run_l5a(**inputs, client=client, state_root=state_root)
    reused = run_l5a(**inputs, client=client, state_root=state_root)

    assert reused.metrics.retry_count == 4
    assert reused.metrics.retry_wait_ms == 12
    require_l5a_publication_receipt(inputs["source"], reused)


@pytest.mark.unit
def test_l5a_readiness_rejects_extra_remote_references(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    state_root = tmp_path / ".fkg" / "l5a"
    run_l5a(**inputs, client=client, state_root=state_root)
    reused = run_l5a(**inputs, client=client, state_root=state_root)
    values = reused.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    values["remote_operation_refs"] = (
        *reused.receipt.remote_operation_refs,
        "remote:forged-extra",
    )
    forged_receipt = StageReceipt(
        **values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )

    with pytest.raises(
        L5aPublicationError,
        match="L5A_PUBLICATION_RECEIPT_INVALID",
    ):
        require_l5a_publication_receipt(
            inputs["source"],
            dataclasses.replace(reused, receipt=forged_receipt),
        )


@pytest.mark.unit
def test_l5a_rejects_authority_and_policy_drift(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    crosswalk = inputs["crosswalks"][0]
    stale_values = crosswalk.model_dump(
        mode="python",
        exclude={"crosswalk_hash"},
    )
    stale_values["hierarchy_hash"] = "f" * 64
    stale = PublicationCrosswalkV1_1(
        **stale_values,
        crosswalk_hash=canonical_sha256(stale_values),
    )
    with pytest.raises(L5aPublicationError, match="L5A_PUBLICATION_CROSSWALK_STALE"):
        compile_l5a_publication(
            **{**inputs, "crosswalks": (stale,)},
        )

    policy = inputs["access_policy"]
    changed_policy = policy.model_dump(mode="python")
    changed_policy["policy_hash"] = "f" * 64
    with pytest.raises(ValueError):
        AccessPolicy.model_validate(changed_policy)


@pytest.mark.unit
def test_l5a_readback_drift_fails_and_cleans_created_resources(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    client.tamper_read_back = "ontology"

    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert raised.value.receipt.status == "failed"
    assert raised.value.receipt.output_manifest_id is None
    assert any(call[0] == "cleanup" for call in client.calls)
    assert not client.states


@pytest.mark.unit
def test_l5a_rejects_misassigned_required_member_readback(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    client.tamper_required_members = "graph"

    with pytest.raises(
        L5aPublicationError,
        match="L5A_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
    ) as raised:
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert raised.value.receipt.status == "failed"


@pytest.mark.unit
def test_l5a_partial_publish_failure_has_no_success_receipt(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    client.fail_publish = "ontology"

    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert raised.value.receipt.status == "failed"
    assert "REMOTE_PUBLISH_FAILED" in raised.value.receipt.error_codes
    assert raised.value.metrics is not None
    assert raised.value.metrics.network_request_bytes > 0
    assert raised.value.metrics.network_response_bytes > 0
    assert ("cleanup", "semantic_model") in client.calls
    assert ("cleanup", "parquet") in client.calls
    assert not client.states


@pytest.mark.unit
def test_l5a_restores_preexisting_targets_after_partial_failure(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    state_root = tmp_path / ".fkg" / "l5a"
    first = run_l5a(**inputs, client=client, state_root=state_root)
    prior_states = dict(client.states)
    shutil.rmtree(first.run_root)
    client.calls.clear()
    client.fail_publish = "ontology"

    with pytest.raises(L5aPublicationError):
        run_l5a(**inputs, client=client, state_root=state_root)

    assert ("restore", "semantic_model") in client.calls
    assert ("restore", "parquet") in client.calls
    assert client.states == prior_states


@pytest.mark.unit
def test_l5a_recovers_ambiguous_create_and_cleans_resource(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    client.raise_after_create = "ontology"

    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert raised.value.receipt.status == "failed"
    assert ("cleanup", "ontology") in client.calls
    assert not client.states


@pytest.mark.unit
def test_l5a_ambiguous_update_counts_publish_and_restore_rows(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    state_root = tmp_path / ".fkg" / "l5a"
    first = run_l5a(**inputs, client=client, state_root=state_root)
    shutil.rmtree(first.run_root)
    prior_states = dict(client.states)
    client.raise_after_create = "ontology"

    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(**inputs, client=client, state_root=state_root)

    assert raised.value.metrics is not None
    rows_per_target = sum(
        item.row_count for item in first.compiled.table_snapshots
    )
    assert raised.value.metrics.fabric_rows_written == rows_per_target * 6
    assert client.states == prior_states


@pytest.mark.unit
def test_l5a_does_not_delete_concurrently_created_resource(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    class _ConcurrentClient(_FakeClient):
        def publish(self, target_kind, target_id, **kwargs):
            operation = super().publish(target_kind, target_id, **kwargs)
            if target_kind == "ontology":
                key = (target_kind, target_id)
                self.states[key] = dataclasses.replace(
                    self.states[key],
                    publication_token="concurrent-publication",
                )
                raise RuntimeError("ambiguous concurrent create")
            return operation

    client = _ConcurrentClient()
    with pytest.raises(L5aPublicationError):
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert ("cleanup", "ontology") not in client.calls
    assert ("ontology", inputs["target_ids"]["ontology"]) in client.states


@pytest.mark.unit
def test_l5a_does_not_cleanup_completed_concurrent_update(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    class _ConcurrentUpdateClient(_FakeClient):
        def publish(self, target_kind, target_id, **kwargs):
            if target_kind == "ontology":
                self.states[(target_kind, target_id)] = L5aTargetState(
                    target_kind=target_kind,
                    target_id=target_id,
                    target_version="1.0.0",
                    definition={},
                    table_snapshots=(),
                    access_policy_id="concurrent",
                    access_policy_hash="f" * 64,
                    publication_token="concurrent-publication",
                    required_member_manifest_rows=(),
                    required_member_rows=(),
                )
            return super().publish(target_kind, target_id, **kwargs)

    client = _ConcurrentUpdateClient()
    client.fail_publish = "graph"
    with pytest.raises(L5aPublicationError):
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert ("cleanup", "ontology") not in client.calls
    assert ("ontology", inputs["target_ids"]["ontology"]) in client.states


@pytest.mark.unit
def test_l5a_recovers_owned_target_after_malformed_returned_operation(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)

    class _MalformedOperationClient(_FakeClient):
        def publish(self, target_kind, target_id, **kwargs):
            operation = super().publish(target_kind, target_id, **kwargs)
            if target_kind == "ontology":
                return dataclasses.replace(operation, target_id="target:wrong")
            return operation

    client = _MalformedOperationClient()
    with pytest.raises(
        L5aPublicationError,
        match="L5A_DEPLOY_OPERATION_MISMATCH",
    ):
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert ("cleanup", "ontology") in client.calls
    assert ("ontology", inputs["target_ids"]["ontology"]) not in client.states


@pytest.mark.unit
def test_l5a_restores_preexisting_target_after_created_flag_mismatch(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)

    class _WrongCreatedFlagClient(_FakeClient):
        def publish(self, target_kind, target_id, **kwargs):
            operation = super().publish(target_kind, target_id, **kwargs)
            if target_kind == "parquet":
                return dataclasses.replace(operation, created=True)
            return operation

    client = _WrongCreatedFlagClient()
    state_root = tmp_path / ".fkg" / "l5a"
    first = run_l5a(**inputs, client=client, state_root=state_root)
    prior = dict(client.states)
    shutil.rmtree(first.run_root)

    with pytest.raises(
        L5aPublicationError,
        match="L5A_DEPLOY_OPERATION_MISMATCH",
    ):
        run_l5a(**inputs, client=client, state_root=state_root)

    assert ("restore", "parquet") in client.calls
    assert client.states == prior


@pytest.mark.unit
def test_l5a_records_malformed_cleanup_as_partial_failure(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    class _MalformedCleanupClient(_FakeClient):
        def cleanup(self, target_kind, target_id, *, publication_token):
            self.calls.append(("cleanup", target_kind))
            return L5aPublishOperation(
                target_kind=target_kind,
                target_id="target:wrong",
                created=False,
                publication_token=publication_token,
                applied=True,
                accounting=self._accounting("cleanup", target_kind),
            )

    client = _MalformedCleanupClient()
    client.fail_publish = "ontology"
    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert "L5A_PARTIAL_CLEANUP_FAILED" in raised.value.receipt.error_codes


@pytest.mark.unit
def test_l5a_records_cleanup_cas_miss_as_partial_failure(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    class _CleanupRaceClient(_FakeClient):
        def cleanup(self, target_kind, target_id, *, publication_token):
            if target_kind == "semantic_model":
                key = (target_kind, target_id)
                self.states[key] = dataclasses.replace(
                    self.states[key],
                    publication_token="concurrent-publication",
                )
            return super().cleanup(
                target_kind,
                target_id,
                publication_token=publication_token,
            )

    client = _CleanupRaceClient()
    client.fail_publish = "ontology"
    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert "L5A_PARTIAL_CLEANUP_FAILED" in raised.value.receipt.error_codes
    assert ("semantic_model", inputs["target_ids"]["semantic_model"]) in client.states


@pytest.mark.unit
def test_l5a_accounting_failure_still_rolls_back_owned_target(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)

    class _BadAccountingClient(_FakeClient):
        def publish(self, target_kind, target_id, **kwargs):
            operation = super().publish(target_kind, target_id, **kwargs)
            if target_kind == "ontology":
                object.__setattr__(
                    operation,
                    "accounting",
                    dataclasses.replace(operation.accounting, request_bytes=-1),
                )
            return operation

    client = _BadAccountingClient()
    with pytest.raises(L5aPublicationError):
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert ("cleanup", "ontology") in client.calls
    assert not client.states


@pytest.mark.unit
def test_l5a_rejects_unsupported_existing_target_before_mutation(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    compiled = compile_l5a_publication(**inputs)
    client = _FakeClient()
    expected = L5aTargetState(
        target_kind="parquet",
        target_id=compiled.target_ids["parquet"],
        target_version="0.9.0",
        definition=compiled.definitions["parquet"],
        table_snapshots=compiled.table_snapshots,
        access_policy_id=compiled.access_policy.access_policy_id,
        access_policy_hash=compiled.access_policy.policy_hash,
        publication_token="stale-publication",
        required_member_manifest_rows=compiled.required_member_manifest_rows,
        required_member_rows=compiled.required_member_rows,
    )
    client.states[("parquet", compiled.target_ids["parquet"])] = expected

    with pytest.raises(L5aPublicationError, match="TARGET_VERSION_UNSUPPORTED"):
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert not any(call[0] == "publish" for call in client.calls)


@pytest.mark.unit
def test_l5a_inspect_without_accounting_fails_closed(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    class _MissingInspectAccounting(_FakeClient):
        def inspect(self, target_kind, target_id):
            self.calls.append(("inspect", target_kind))
            return self.states.get((target_kind, target_id))

    client = _MissingInspectAccounting()
    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert "L5A_REMOTE_ACCOUNTING_MISSING" in (
        raised.value.receipt.error_codes
    )
    assert raised.value.metrics is not None
    assert raised.value.metrics.fabric_calls == 1


@pytest.mark.unit
def test_l5a_custom_adapter_exception_records_missing_accounting(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)

    class _CustomExceptionClient(_FakeClient):
        def inspect(self, target_kind, target_id):
            self.calls.append(("inspect", target_kind))
            raise KeyError("adapter decode failure")

    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=_CustomExceptionClient(),
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert "L5A_REMOTE_ACCOUNTING_MISSING" in (
        raised.value.receipt.error_codes
    )


@pytest.mark.unit
def test_l5a_adapter_contract_error_without_result_is_unaccounted(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)

    class _ContractErrorClient(_FakeClient):
        def inspect(self, target_kind, target_id):
            raise L5aPublicationError(
                "ADAPTER_REPORTED_ERROR",
                "raised before returning accounting",
            )

    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=_ContractErrorClient(),
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert "L5A_REMOTE_ACCOUNTING_MISSING" in (
        raised.value.receipt.error_codes
    )
    assert "ADAPTER_REPORTED_ERROR" not in raised.value.receipt.error_codes


@pytest.mark.unit
def test_l5a_reused_remote_reference_fails_before_success(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    class _ReusedReferenceClient(_FakeClient):
        def _accounting(self, verb, target_kind, *, errors=()):
            return L5aRemoteAccounting(
                operation_refs=("remote:reused",),
                request_bytes=11,
                response_bytes=7,
                retry_count=0,
                retry_wait_ms=0,
                error_codes=tuple(errors),
            )

    client = _ReusedReferenceClient()
    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert "L5A_REMOTE_REFERENCE_REUSED" in raised.value.receipt.error_codes


@pytest.mark.unit
def test_l5a_readback_malformed_accounting_fails_closed(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)

    class _MalformedReadbackAccounting(_FakeClient):
        def read_back(self, target_kind, target_id):
            operation = super().read_back(target_kind, target_id)
            if target_kind == "graph":
                return dataclasses.replace(
                    operation,
                    accounting=dataclasses.replace(
                        operation.accounting,
                        response_bytes=0,
                    ),
                )
            return operation

    client = _MalformedReadbackAccounting()
    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.receipt is not None
    assert "L5A_REMOTE_ACCOUNTING_INVALID" in (
        raised.value.receipt.error_codes
    )
    assert not client.states


@pytest.mark.unit
def test_l5a_valid_accounting_survives_malformed_state_payload(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)

    class _MalformedStateClient(_FakeClient):
        def inspect(self, target_kind, target_id):
            self.calls.append(("inspect", target_kind))
            return L5aStateOperation(
                state="invalid",  # type: ignore[arg-type]
                accounting=self._accounting("inspect", target_kind),
            )

    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(
            **inputs,
            client=_MalformedStateClient(),
            state_root=tmp_path / ".fkg" / "l5a",
        )

    assert raised.value.metrics is not None
    assert raised.value.metrics.network_request_bytes == 11
    assert raised.value.metrics.network_response_bytes == 7
    assert raised.value.receipt is not None
    assert "L5A_REMOTE_STATE_INVALID" in raised.value.receipt.error_codes
    assert len(raised.value.receipt.remote_operation_refs) == 2


@pytest.mark.unit
def test_l5a_local_tamper_prevents_checkpoint_reuse(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    state_root = tmp_path / ".fkg" / "l5a"
    first = run_l5a(**inputs, client=client, state_root=state_root)
    definition = first.run_root / "definitions" / "graph.json"
    definition.write_text(definition.read_text("utf-8") + " ", encoding="utf-8")
    client.calls.clear()

    repaired = run_l5a(**inputs, client=client, state_root=state_root)

    assert not repaired.reused
    assert repaired.receipt.status == "succeeded"
    assert sum(call[0] == "publish" for call in client.calls) == 4


@pytest.mark.unit
def test_l5a_reuse_readback_without_accounting_fails_closed(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    state_root = tmp_path / ".fkg" / "l5a"
    run_l5a(**inputs, client=client, state_root=state_root)
    client.raise_read_back = "parquet"
    client.calls.clear()

    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(**inputs, client=client, state_root=state_root)

    assert raised.value.receipt is not None
    assert raised.value.receipt.status == "failed"
    assert "L5A_REMOTE_ACCOUNTING_MISSING" in (
        raised.value.receipt.error_codes
    )
    assert raised.value.metrics is not None
    assert raised.value.metrics.fabric_calls == 17
    assert sum(call[0] == "publish" for call in client.calls) == 4


@pytest.mark.unit
def test_l5a_remote_drift_invalidates_reuse_and_republishes(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    state_root = tmp_path / ".fkg" / "l5a"
    run_l5a(**inputs, client=client, state_root=state_root)
    client.states.pop(("graph", inputs["target_ids"]["graph"]))
    client.calls.clear()

    repaired = run_l5a(**inputs, client=client, state_root=state_root)

    assert not repaired.reused
    assert repaired.receipt.status == "succeeded"
    assert repaired.metrics.fabric_calls == 16
    assert sum(call[0] == "publish" for call in client.calls) == 4


@pytest.mark.unit
def test_l5a_worst_case_state_machine_is_exactly_bounded(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    state_root = tmp_path / ".fkg" / "l5a"
    run_l5a(**inputs, client=client, state_root=state_root)
    client.tamper_required_members = "graph"
    client.calls.clear()

    with pytest.raises(L5aPublicationError) as raised:
        run_l5a(**inputs, client=client, state_root=state_root)

    assert raised.value.metrics is not None
    assert raised.value.metrics.fabric_calls == L5A_MAX_FABRIC_CALLS == 20
    assert not raised.value.metrics.exceeded_dimensions
    assert len(client.calls) == L5A_MAX_FABRIC_CALLS
    assert sum(call[0] == "restore" for call in client.calls) == 4


@pytest.mark.unit
def test_l5a_stops_before_exceeding_remote_call_budget() -> None:
    accounting = _CallAccounting()
    for _ in range(L5A_MAX_FABRIC_CALLS):
        accounting.begin_call()

    with pytest.raises(
        L5aPublicationError,
        match="L5A_CALL_BUDGET_EXCEEDED",
    ):
        accounting.begin_call()

    assert accounting.fabric_calls == L5A_MAX_FABRIC_CALLS
    assert accounting.exceeded_dimensions == ("fabric_calls",)


@pytest.mark.unit
def test_l5a_readiness_rejects_persisted_artifact_tamper(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    result = run_l5a(
        **inputs,
        client=_FakeClient(),
        state_root=tmp_path / ".fkg" / "l5a",
    )
    graph = result.run_root / "definitions" / "graph.json"
    graph.write_text(graph.read_text("utf-8") + " ", encoding="utf-8")

    with pytest.raises(
        L5aPublicationError,
        match="L5A_PUBLICATION_RECEIPT_INVALID",
    ):
        require_l5a_publication_receipt(inputs["source"], result)


@pytest.mark.unit
def test_l5a_readiness_rejects_forged_receipt_metrics(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    result = run_l5a(
        **inputs,
        client=_FakeClient(),
        state_root=tmp_path / ".fkg" / "l5a",
    )
    metric_values = result.metrics.model_dump(
        mode="python",
        exclude={"metrics_hash"},
    )
    metric_values["network_request_bytes"] = 999999
    forged_metrics = StageResourceMetrics(
        **metric_values,
        metrics_hash=canonical_sha256(metric_values),
    )
    receipt_values = result.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values["resource_metrics_hash"] = forged_metrics.metrics_hash
    forged_receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )
    forged = dataclasses.replace(
        result,
        metrics=forged_metrics,
        receipt=forged_receipt,
    )

    with pytest.raises(
        L5aPublicationError,
        match="L5A_PUBLICATION_RECEIPT_INVALID",
    ):
        require_l5a_publication_receipt(inputs["source"], forged)


@pytest.mark.unit
def test_l5a_skipped_readiness_does_not_make_unaccounted_calls(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    state_root = tmp_path / ".fkg" / "l5a"
    run_l5a(**inputs, client=client, state_root=state_root)
    reused = run_l5a(**inputs, client=client, state_root=state_root)
    client.calls.clear()

    require_l5a_publication_receipt(inputs["source"], reused)
    assert client.calls == []


@pytest.mark.unit
def test_l5a_rejects_unanchored_governed_asset(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    asset = inputs["governed_assets"][0]
    asset_values = asset.model_dump(
        mode="python",
        exclude={"asset_reference_hash"},
    )
    identity_values = asset.identity.model_dump(mode="python")
    identity_values["parent_artifact_ids"] = ()
    asset_values["identity"] = CanonicalIdentityEnvelope.model_validate(
        identity_values
    )
    unanchored = GovernedAssetReference(
        **asset_values,
        asset_reference_hash=canonical_sha256(asset_values),
    )

    with pytest.raises(
        L5aPublicationError,
        match="L5A_GOVERNED_ASSET_AUTHORITY_MISMATCH",
    ):
        assets = (
            unanchored,
            *inputs["governed_assets"][1:],
        )
        compile_l5a_publication(
            **{**inputs, "governed_assets": assets},
        )


@pytest.mark.unit
def test_l5a_rejects_reserved_physical_column_collision(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    crosswalk = inputs["crosswalks"][0]
    values = crosswalk.model_dump(mode="python", exclude={"crosswalk_hash"})
    first_type = values["semantic_type_mappings"][0]
    first_type["physical_property_bindings"][0][
        "physical_column_id"
    ] = "__canonical_id"
    changed = PublicationCrosswalkV1_1(
        **values,
        crosswalk_hash=canonical_sha256(values),
    )

    with pytest.raises(
        L5aPublicationError,
        match="L5A_RESERVED_COLUMN_COLLISION",
    ):
        compile_l5a_publication(
            **{**inputs, "crosswalks": (changed,)},
        )


@pytest.mark.unit
def test_l5a_rejects_forged_parent_with_coordinated_caller_reseals(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    crosswalk = inputs["crosswalks"][0]
    child = crosswalk.semantic_type_mappings[1]
    forged_child = child.model_copy(update={
        "canonical_parent_semantic_type_id": (
            crosswalk.semantic_type_mappings[0].canonical_semantic_type_id
        ),
    })
    values = crosswalk.model_dump(mode="python", exclude={"crosswalk_hash"})
    values["semantic_type_mappings"] = (
        crosswalk.semantic_type_mappings[0],
        forged_child,
    )
    forged = PublicationCrosswalkV1_1(
        **values,
        crosswalk_hash=canonical_sha256(values),
    )
    with pytest.raises(
        L5aPublicationError,
        match="L5A_TYPE_AUTHORITY_MISMATCH",
    ):
        _assets(
            inputs["source"],
            forged,
            inputs["access_policy"],
            inputs["target_ids"],
        )


@pytest.mark.unit
def test_l5a_rejects_physical_name_shadow_of_carried_authority(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    crosswalk = inputs["crosswalks"][0]
    values = crosswalk.model_dump(mode="python", exclude={"crosswalk_hash"})
    values["semantic_type_mappings"][0]["physical_table_id"] = (
        "l4_semantic_publication_authority"
    )
    changed = PublicationCrosswalkV1_1(
        **values,
        crosswalk_hash=canonical_sha256(values),
    )

    with pytest.raises(
        L5aPublicationError,
        match="L5A_PHYSICAL_TABLE_COLLISION",
    ):
        compile_l5a_publication(
            **{**inputs, "crosswalks": (changed,)},
        )


@pytest.mark.unit
@pytest.mark.parametrize("outcome", ("success", "reuse", "failure"))
def test_l5a_cpu_metrics_are_stage_elapsed_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    inputs = _inputs(tmp_path)
    client = _FakeClient()
    state_root = tmp_path / ".fkg" / "l5a"
    if outcome == "reuse":
        run_l5a(**inputs, client=client, state_root=state_root)
    elif outcome == "failure":
        client.fail_publish = "ontology"
    process_times = iter((10_000.0, 10_000.012))
    monkeypatch.setattr(
        "fabric_kg_builder.serving.structured_publication.time.process_time",
        lambda: next(process_times),
    )

    if outcome == "failure":
        with pytest.raises(L5aPublicationError) as raised:
            run_l5a(**inputs, client=client, state_root=state_root)
        metrics = raised.value.metrics
        assert metrics is not None
    else:
        metrics = run_l5a(
            **inputs,
            client=client,
            state_root=state_root,
        ).metrics

    assert metrics.cpu_ms == 12


@pytest.mark.unit
def test_l5a_schema1_and_later_layers_remain_inactive(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    with pytest.raises(L5aPublicationError, match="L5A_SCHEMA2_SOURCE_REQUIRED"):
        compile_l5a_publication(
            **{**inputs, "source": tmp_path},
        )

    source = Path(
        "src/fabric_kg_builder/serving/structured_publication.py"
    ).read_text("utf-8")
    assert "search_calls" in source
    assert "search_documents_written" in source
    assert '"search"' not in source.split("L5A_TARGET_ORDER =", 1)[1].splitlines()[0]
