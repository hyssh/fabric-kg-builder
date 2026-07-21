"""Contract tests for the SPEC-008 canonical semantic authority."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from fabric_kg_builder.cli import cli
from fabric_kg_builder.cli.build_deploy_cmd import (
    BuildDeployError,
    _semantic_compatibility_gate,
)
from fabric_kg_builder.cli.semantic_cmd import (
    compile_agent_cmd,
    compile_graph_cmd,
    compile_semantic_cmd,
)
from fabric_kg_builder.ontology.compiler import OntologyCompiler
from fabric_kg_builder.release.diagnostics import inspect_files
from fabric_kg_builder.runtime import (
    RuntimeConfig,
    RuntimeEvidenceCollector,
    build_runtime_report,
    evaluate_runtime_evidence,
    load_competency_contract,
    validate_deployment_evidence,
)
from fabric_kg_builder.semantic import (
    ApprovalMetadata,
    AssertionPolicy,
    Cardinality,
    CompatibilityLevel,
    EntityMapping,
    EntityTypeDefinition,
    InverseDefinition,
    PhysicalMappings,
    PersistedQuerySchema,
    PropertyDefinition,
    RelationshipMapping,
    RelationshipTypeDefinition,
    SemanticContract,
    SemanticContractCompatibilityError,
    SemanticCompileError,
    SemanticArtifactValidationError,
    SemanticContractValidationError,
    StableIdBinding,
    StableIdLock,
    Vocabulary,
    VocabularyTerm,
    compute_semantic_contract_hash,
    compute_persisted_query_schema_hash,
    build_contract_agent_instructions,
    classify_contract_change,
    compile_semantic_bundle,
    import_legacy_id_lock,
    load_semantic_bundle,
    load_semantic_contract,
    load_semantic_model_artifacts,
    normalize_semantic_contract,
    validate_approved_contract,
    validate_compiled_semantic_artifacts,
    validate_ontology_projection_parts,
    validate_semantic_bundle,
)
from fabric_kg_builder.serving.graph_model import build_graph_model_parts

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "semantic_contracts"
)


class _StaticRuntimeExecutor:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def execute(self, _case: Any) -> dict[str, Any]:
        return dict(self._result)


def _entity(
    semantic_id: str,
    name: str,
    *,
    parent: str | None = None,
    aliases: list[str] | None = None,
) -> EntityTypeDefinition:
    return EntityTypeDefinition(
        id=semantic_id,
        name=name,
        business_name=name,
        description=f"Business definition for {name}.",
        parent=parent,
        identifiers=["entity_id"],
        aliases=aliases or [],
        properties=[
            PropertyDefinition(name="entity_id", type="string", required=True),
            PropertyDefinition(name="display_name", type="string", required=True),
        ],
        lineage_properties=["asset_id", "asset_version_id", "run_id"],
        publication_status="core",
    )


def _contract(domain: str = "supply-chain") -> SemanticContract:
    root = _entity("entity-type:business-object", "BusinessObject")
    subject = _entity(
        f"entity-type:{domain}-subject",
        "Subject",
        parent=root.id,
        aliases=["Tracked item"],
    )
    event = _entity(
        f"entity-type:{domain}-event",
        "Event",
        parent=root.id,
    )
    relationship = RelationshipTypeDefinition(
        id=f"relationship-type:{domain}-has-event",
        predicate="has_event",
        business_name="has event",
        description="A subject is associated with an evidence-backed event.",
        source_type=subject.id,
        target_type=event.id,
        inverse=InverseDefinition(predicate="event_of", materialization="virtual"),
        cardinality=Cardinality(source="many", target="many"),
        evidence_policy="required_for_asserted",
        assertion_policy=AssertionPolicy(
            allowed_statuses=["asserted", "unresolved"],
            default_status="unresolved",
        ),
        temporal="optional",
        publication_status="core",
    )
    return SemanticContract(
        contract_version="1.0.0",
        name=f"{domain} semantic contract",
        description=f"Synthetic {domain} ontology for contract tests.",
        entity_types=[root, subject, event],
        relationship_types=[relationship],
    )


def _mappings(contract: SemanticContract) -> PhysicalMappings:
    return PhysicalMappings(
        entity_types=[
            EntityMapping(
                semantic_id=entity.id,
                table="semantic_entities",
                entity_id_column="entity_id",
                display_name_column="display_name",
                property_columns={
                    "entity_id": "entity_id",
                    "display_name": "display_name",
                },
                type_filter_column="entity_type",
                type_filter_value=entity.name,
            )
            for entity in contract.entity_types
        ],
        relationship_types=[
            RelationshipMapping(
                semantic_id=relationship.id,
                table="semantic_relationships",
                relationship_id_column="relationship_id",
                source_entity_id_column="source_entity_id",
                target_entity_id_column="target_entity_id",
                evidence_id_column="evidence_id",
                type_filter_column="relationship_type",
                type_filter_value=relationship.predicate,
            )
            for relationship in contract.relationship_types
        ],
    )


def _ids(contract: SemanticContract) -> StableIdLock:
    return StableIdLock(
        entity_types={
            entity.name: StableIdBinding(
                semantic_id=entity.id,
                fabric_id=str(1000 + index),
            )
            for index, entity in enumerate(contract.entity_types)
        },
        relationship_types={
            relationship.predicate: StableIdBinding(
                semantic_id=relationship.id,
                fabric_id=str(2000 + index),
            )
            for index, relationship in enumerate(contract.relationship_types)
        },
    )


def _vocabulary() -> Vocabulary:
    return Vocabulary(
        terms=[
            VocabularyTerm(
                id="term:subject",
                preferred_label="Subject",
                definition="The primary tracked business object.",
                aliases=["Item"],
            )
        ]
    )


def _approve(contract: SemanticContract) -> SemanticContract:
    contract_hash = compute_semantic_contract_hash(contract)
    return contract.model_copy(
        update={
            "approval": ApprovalMetadata(
                status="approved",
                approved_by="test-reviewer",
                approved_at_utc="2026-07-17T00:00:00Z",
                contract_hash=contract_hash,
            )
        }
    )


def _write_bundle(
    root: Path,
    contract: SemanticContract,
) -> tuple[Path, Path, Path, Path]:
    contract_path = root / "contract.yaml"
    mappings_path = root / "mappings.yaml"
    vocabulary_path = root / "vocabulary.yaml"
    lock_path = root / "ids.lock.json"
    contract_path.write_text(
        yaml.safe_dump(contract.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    mappings_path.write_text(
        yaml.safe_dump(_mappings(contract).model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    vocabulary_path.write_text(
        yaml.safe_dump(_vocabulary().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    lock_path.write_text(
        json.dumps(_ids(contract).model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    return contract_path, mappings_path, vocabulary_path, lock_path


@pytest.mark.parametrize(
    "domain",
    ["supply-chain", "clinical-operations", "legal-compliance"],
)
def test_three_unrelated_domains_validate(domain: str) -> None:
    contract = _approve(_contract(domain))
    assert validate_semantic_bundle(
        contract,
        _mappings(contract),
        _vocabulary(),
        _ids(contract),
    ).startswith("sha256:")


@pytest.mark.parametrize(
    "fixture_name",
    ["supply-chain.yaml", "clinical-operations.yaml", "legal-compliance.yaml"],
)
def test_unrelated_semantic_contract_fixtures_are_valid(
    fixture_name: str,
) -> None:
    contract = load_semantic_contract(_FIXTURE_DIR / fixture_name)
    assert contract.approval.status == "draft"
    assert contract.entity_types
    assert contract.relationship_types


def test_normalization_and_hash_ignore_definition_order() -> None:
    first = _contract()
    second = first.model_copy(
        update={
            "entity_types": list(reversed(first.entity_types)),
            "relationship_types": list(reversed(first.relationship_types)),
        }
    )
    assert normalize_semantic_contract(first) == normalize_semantic_contract(second)
    assert compute_semantic_contract_hash(first) == compute_semantic_contract_hash(
        second
    )


def test_approval_metadata_does_not_change_contract_hash() -> None:
    contract = _contract()
    approved = _approve(contract)
    assert compute_semantic_contract_hash(contract) == compute_semantic_contract_hash(
        approved
    )


def test_approved_contract_requires_current_hash() -> None:
    contract = _contract().model_copy(
        update={
            "approval": ApprovalMetadata(
                status="approved",
                approved_by="reviewer",
                approved_at_utc="2026-07-17T00:00:00Z",
                contract_hash="sha256:stale",
            )
        }
    )
    with pytest.raises(SemanticContractValidationError, match="stale"):
        validate_approved_contract(contract)


def test_unknown_relationship_target_fails() -> None:
    contract = _contract()
    bad_relationship = contract.relationship_types[0].model_copy(
        update={"target_type": "entity-type:missing"}
    )
    with pytest.raises(ValidationError, match="unknown target"):
        SemanticContract(
            contract_version=contract.contract_version,
            name=contract.name,
            description=contract.description,
            entity_types=contract.entity_types,
            relationship_types=[bad_relationship],
        )


def test_identifier_must_reference_property() -> None:
    with pytest.raises(ValidationError, match="unknown properties"):
        EntityTypeDefinition(
            id="entity-type:asset",
            name="Asset",
            business_name="Asset",
            description="Tracked asset.",
            identifiers=["missing_id"],
            properties=[
                PropertyDefinition(
                    name="entity_id", type="string", required=True
                )
            ],
        )


def test_id_lock_remap_is_rejected() -> None:
    contract = _approve(_contract())
    ids = _ids(contract)
    subject = contract.entity_types[1]
    ids.entity_types[subject.name] = StableIdBinding(
        semantic_id="entity-type:wrong-subject",
        fabric_id=ids.entity_types[subject.name].fabric_id,
    )
    with pytest.raises(SemanticContractValidationError, match="remaps"):
        validate_semantic_bundle(
            contract,
            _mappings(contract),
            _vocabulary(),
            ids,
        )


def test_unknown_physical_mapping_is_rejected() -> None:
    contract = _approve(_contract())
    mappings = _mappings(contract)
    mappings.entity_types.append(
        EntityMapping(
            semantic_id="entity-type:unknown",
            table="unknown",
            entity_id_column="entity_id",
            display_name_column="display_name",
        )
    )
    with pytest.raises(SemanticContractValidationError, match="unknown semantic IDs"):
        validate_semantic_bundle(
            contract,
            mappings,
            _vocabulary(),
            _ids(contract),
        )


def test_legacy_numeric_id_lock_requires_explicit_migration(tmp_path: Path) -> None:
    contract = _approve(_contract())
    contract_path = tmp_path / "contract.yaml"
    mappings_path = tmp_path / "mappings.yaml"
    vocabulary_path = tmp_path / "vocabulary.yaml"
    lock_path = tmp_path / "ids.lock.json"
    contract_path.write_text(
        yaml.safe_dump(contract.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    mappings_path.write_text(
        yaml.safe_dump(_mappings(contract).model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    vocabulary_path.write_text(
        yaml.safe_dump(_vocabulary().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    lock_path.write_text(
        json.dumps({"entityTypes": {"Subject": "1001"}, "relationshipTypes": {}}),
        encoding="utf-8",
    )
    with pytest.raises(SemanticContractCompatibilityError, match="Legacy"):
        load_semantic_bundle(
            contract_path=contract_path,
            mappings_path=mappings_path,
            vocabulary_path=vocabulary_path,
            ids_lock_path=lock_path,
        )


def test_complete_bundle_loads_from_yaml_and_json(tmp_path: Path) -> None:
    contract = _approve(_contract())
    contract_path, mappings_path, vocabulary_path, lock_path = _write_bundle(
        tmp_path, contract
    )

    bundle = load_semantic_bundle(
        contract_path=contract_path,
        mappings_path=mappings_path,
        vocabulary_path=vocabulary_path,
        ids_lock_path=lock_path,
    )

    assert bundle.contract_hash == compute_semantic_contract_hash(contract)
    assert bundle.contract.name == contract.name


def test_inspect_ontology_reports_compile_ready_bundle(tmp_path: Path) -> None:
    contract = _approve(_contract())
    contract_path, mappings_path, vocabulary_path, lock_path = _write_bundle(
        tmp_path, contract
    )
    result = CliRunner().invoke(
        cli,
        [
            "inspect-ontology",
            "--contract",
            str(contract_path),
            "--mappings",
            str(mappings_path),
            "--vocabulary",
            str(vocabulary_path),
            "--ids-lock",
            str(lock_path),
            "--format",
            "json",
            "--require-approved",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ready_for_compile"] is True
    assert report["contract_hash"] == compute_semantic_contract_hash(contract)
    assert report["core_relationship_types"][0]["predicate"] == "has_event"


def test_inspect_ontology_allows_draft_but_reports_warning(tmp_path: Path) -> None:
    contract = _contract()
    contract_path, mappings_path, vocabulary_path, lock_path = _write_bundle(
        tmp_path, contract
    )
    result = CliRunner().invoke(
        cli,
        [
            "inspect-ontology",
            "--contract",
            str(contract_path),
            "--mappings",
            str(mappings_path),
            "--vocabulary",
            str(vocabulary_path),
            "--ids-lock",
            str(lock_path),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ready_for_compile"] is False
    assert report["findings"][0]["code"] == "SEM_APPROVAL_REQUIRED"


def test_compatibility_classifier_detects_breaking_endpoint_change() -> None:
    previous = _contract()
    relationship = previous.relationship_types[0].model_copy(
        update={"target_type": previous.entity_types[0].id}
    )
    current = previous.model_copy(
        update={
            "contract_version": "2.0.0",
            "relationship_types": [relationship],
        }
    )
    report = classify_contract_change(previous, current)
    assert report.level == CompatibilityLevel.BREAKING
    assert any(
        change.code == "RELATIONSHIP_TARGET_TYPE_CHANGED"
        for change in report.changes
    )


def test_live_semantic_gate_requires_explicit_breaking_approval(
    tmp_path: Path,
) -> None:
    previous = _approve(_contract())
    current = _approve(
        _contract().model_copy(update={"relationship_types": []})
    )
    previous_path = tmp_path / "previous.yaml"
    current_path = tmp_path / "current.yaml"
    report_path = tmp_path / "compatibility.json"
    previous_path.write_text(
        yaml.safe_dump(previous.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    current_path.write_text(
        yaml.safe_dump(current.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(BuildDeployError, match="approve-breaking"):
        _semantic_compatibility_gate(
            current_contract_path=current_path,
            baseline_contract_path=previous_path,
            report_path=report_path,
            approve_breaking_migration=False,
            initialize_baseline=False,
            enforce=True,
        )

    report = _semantic_compatibility_gate(
        current_contract_path=current_path,
        baseline_contract_path=previous_path,
        report_path=report_path,
        approve_breaking_migration=True,
        initialize_baseline=False,
        enforce=True,
    )

    assert report["level"] == "breaking"
    assert report["breaking_migration_approved"] is True


def test_live_semantic_gate_requires_explicit_baseline_initialization(
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current.yaml"
    missing_baseline_path = tmp_path / "missing.yaml"
    report_path = tmp_path / "compatibility.json"
    current_path.write_text(
        yaml.safe_dump(
            _approve(_contract()).model_dump(mode="json"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(BuildDeployError, match="previous-semantic-contract"):
        _semantic_compatibility_gate(
            current_contract_path=current_path,
            baseline_contract_path=missing_baseline_path,
            report_path=report_path,
            approve_breaking_migration=False,
            initialize_baseline=False,
            enforce=True,
        )

    blocked = json.loads(report_path.read_text(encoding="utf-8"))
    assert blocked["level"] == "baseline_missing"
    assert blocked["baseline_initialization_approved"] is False

    initialized = _semantic_compatibility_gate(
        current_contract_path=current_path,
        baseline_contract_path=missing_baseline_path,
        report_path=report_path,
        approve_breaking_migration=False,
        initialize_baseline=True,
        enforce=True,
    )

    assert initialized["level"] == "baseline"
    assert initialized["baseline_initialization_approved"] is True


def test_compatibility_classifier_allows_optional_property_addition() -> None:
    previous = _contract()
    subject = previous.entity_types[1]
    current_subject = subject.model_copy(
        update={
            "properties": [
                *subject.properties,
                PropertyDefinition(name="category", type="string"),
            ]
        }
    )
    current = previous.model_copy(
        update={
            "contract_version": "1.1.0",
            "entity_types": [
                previous.entity_types[0],
                current_subject,
                previous.entity_types[2],
            ],
        }
    )
    report = classify_contract_change(previous, current)
    assert report.level == CompatibilityLevel.COMPATIBLE
    assert report.changes[0].code == "PROPERTY_ADDED"


def test_legacy_id_import_preserves_matching_fabric_ids(tmp_path: Path) -> None:
    contract = _contract()
    legacy_path = tmp_path / "legacy.ids.lock.json"
    legacy_path.write_text(
        json.dumps(
            {
                "entityTypes": {
                    entity.name: str(1000 + index)
                    for index, entity in enumerate(contract.entity_types)
                },
                "relationshipTypes": {
                    relationship.predicate: str(2000 + index)
                    for index, relationship in enumerate(
                        contract.relationship_types
                    )
                },
                "properties": {},
            }
        ),
        encoding="utf-8",
    )
    result = import_legacy_id_lock(legacy_path, contract)
    assert result.ids.entity_types["Subject"].fabric_id == "1001"
    assert (
        result.ids.relationship_types["has_event"].semantic_id
        == contract.relationship_types[0].id
    )
    assert result.unmapped_legacy_entity_types == ()


def test_legacy_id_import_requires_disposition_for_old_semantics(
    tmp_path: Path,
) -> None:
    contract = _contract()
    legacy_path = tmp_path / "legacy.ids.lock.json"
    legacy_path.write_text(
        json.dumps(
            {
                "entityTypes": {
                    **{entity.name: str(1000 + index) for index, entity in enumerate(contract.entity_types)},
                    "LegacySurfaceOnly": "1999",
                },
                "relationshipTypes": {
                    relationship.predicate: str(2000 + index)
                    for index, relationship in enumerate(
                        contract.relationship_types
                    )
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        SemanticContractCompatibilityError,
        match="Classify them",
    ):
        import_legacy_id_lock(legacy_path, contract)


def test_shared_compiler_emits_exact_ontology_graph_and_agent_semantics(
    tmp_path: Path,
) -> None:
    contract = _approve(_contract())
    bundle = load_semantic_bundle(
        contract_path=_write_bundle(tmp_path, contract)[0],
        mappings_path=tmp_path / "mappings.yaml",
        vocabulary_path=tmp_path / "vocabulary.yaml",
        ids_lock_path=tmp_path / "ids.lock.json",
    )
    compiled = compile_semantic_bundle(bundle, ontology_name="SyntheticOntology")
    relationship = compiled.ontology_model["relationshipTypes"][0]
    assert relationship["name"] == "has_event"
    assert relationship["sourceType"] == "Subject"
    assert relationship["targetType"] == "Event"

    assert compiled.graph_node_labels["Subject"] == "Subject"
    assert compiled.graph_relationships[0]["graph_label"] == "has_event"
    assert (
        compiled.agent_semantic_context["relationship_types"][0]["graph_label"]
        == "has_event"
    )

    compiled.write(tmp_path / "compiled")
    catalog = json.loads(
        (tmp_path / "compiled" / "graph" / "label-catalog.json").read_text(
            encoding="utf-8"
        )
    )
    assert catalog["contract_hash"] == bundle.contract_hash
    assert catalog["edges"][0]["source_graph_label"] == "Subject"
    manifest = json.loads(
        (tmp_path / "compiled" / "semantic-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    model_manifest = json.loads(
        (
            tmp_path / "compiled" / "semantic-model-manifest.json"
        ).read_text(encoding="utf-8")
    )
    crosswalk = json.loads(
        (tmp_path / "compiled" / "semantic-crosswalk.json").read_text(
            encoding="utf-8"
        )
    )
    materialization = json.loads(
        (tmp_path / "compiled" / "materialization-plan.json").read_text(
            encoding="utf-8"
        )
    )
    model_quality = json.loads(
        (tmp_path / "compiled" / "model-quality-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert model_manifest["manifest_hash"].startswith("sha256:")
    assert crosswalk["manifest_hash"] == model_manifest["manifest_hash"]
    assert materialization["manifest_hash"] == model_manifest["manifest_hash"]
    assert {
        entry["semantic_id"] for entry in crosswalk["entity_type_entries"]
    } == {entity.id for entity in contract.entity_types}
    assert all(
        table["source_table_name"] == "semantic_entities"
        for table in materialization["entity_tables"]
    )
    assert all(
        table["table_name"].startswith("kg_entity_")
        for table in materialization["entity_tables"]
    )
    assert (
        model_manifest["model_quality"][
            "accepted_property_evidence_coverage"
        ]
        == 0.0
    )
    assert (
        model_manifest["model_quality"][
            "accepted_relationship_evidence_coverage"
        ]
        == 0.0
    )
    assert model_quality["source_quality_report_hash"] is None
    assert {
        finding["code"] for finding in model_quality["findings"]
    } >= {"SOURCE_QUALITY_EVIDENCE_NOT_PROVIDED"}
    assert manifest["artifact_set_hash"].startswith("sha256:")
    for artifact in manifest["artifacts"]:
        path = tmp_path / "compiled" / artifact["path"]
        assert path.exists()
        assert artifact["sha256"].startswith("sha256:")

    ontology_compiler = OntologyCompiler(
        model_path=tmp_path / "compiled" / "ontology" / "model.yaml",
        ids_lock_path=tmp_path / "compiled" / "ontology" / "ids.lock.json",
        lakehouse_id="synthetic-lakehouse",
    )
    ontology_parts = ontology_compiler.get_rest_parts()
    assert len(ontology_parts) == 9
    expected_binding_ids = {
        entity.ontology_projection.binding_id
        for entity in compiled.semantic_model_manifest.entity_types
    }
    assert expected_binding_ids == {
        Path(part["path"]).stem
        for part in ontology_parts
        if "/DataBindings/" in part["path"]
    }
    assert set(compiled.ontology_ids_lock["properties"]) == {
        f"{prop.owner_type_id}/{prop.property_id}"
        for prop in compiled.semantic_model_manifest.property_definitions
    }


def test_ontology_identity_uses_physical_relationship_key(
    tmp_path: Path,
) -> None:
    contract = _contract()
    subject = contract.entity_types[1]
    subject.identifiers = ["canonical_key"]
    subject.properties.append(
        PropertyDefinition(
            name="canonical_key",
            type="string",
            required=True,
        )
    )
    subject.properties.append(
        PropertyDefinition(name="observed_date", type="date")
    )
    approved = _approve(contract)
    paths = _write_bundle(tmp_path, approved)

    compiled = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=paths[0],
            mappings_path=paths[1],
            vocabulary_path=paths[2],
            ids_lock_path=paths[3],
        )
    )
    subject_projection = next(
        entity
        for entity in compiled.ontology_model["entityTypes"]
        if entity["name"] == "Subject"
    )

    assert subject_projection["entityIdProperties"] == ["entity_id"]
    observed_date = next(
        prop
        for prop in subject_projection["properties"]
        if prop["name"] == "observed_date"
    )
    assert observed_date["type"] == "string"
    compiled.write(tmp_path / "compiled")
    ontology_parts = OntologyCompiler(
        model_path=tmp_path / "compiled" / "ontology" / "model.yaml",
        ids_lock_path=tmp_path / "compiled" / "ontology" / "ids.lock.json",
        lakehouse_id="synthetic-lakehouse",
    ).get_rest_parts()
    findings = validate_ontology_projection_parts(
        {
            part["path"]: json.loads(
                base64.b64decode(part["payload"]).decode("utf-8")
            )
            for part in ontology_parts
        },
        compiled.semantic_model_manifest,
        compiled.materialization_plan,
    )
    assert findings == []


def test_reserved_fabric_type_name_compiles_to_safe_physical_name(
    tmp_path: Path,
) -> None:
    contract = _contract()
    subject = contract.entity_types[1].model_copy(
        update={"name": "Project", "business_name": "Project"}
    )
    contract = _approve(contract.model_copy(update={
        "entity_types": [
            contract.entity_types[0],
            subject,
            contract.entity_types[2],
        ]
    }))
    paths = _write_bundle(tmp_path, contract)

    compiled = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=paths[0],
            mappings_path=paths[1],
            vocabulary_path=paths[2],
            ids_lock_path=paths[3],
        )
    )
    project = next(
        entity
        for entity in compiled.semantic_model_manifest.entity_types
        if entity.semantic_id == subject.id
    )

    assert project.canonical_name == "ProjectEntity"
    assert project.graph_projection.label == "ProjectEntity"
    assert project.business_name == "Project"
    assert "Project" in project.aliases
    assert "ProjectEntity" in {
        entity["name"] for entity in compiled.ontology_model["entityTypes"]
    }


def test_compile_rejects_dangling_materialized_inverse(tmp_path: Path) -> None:
    contract = _contract()
    forward = contract.relationship_types[0].model_copy(
        update={
            "inverse": InverseDefinition(
                predicate="event_of",
                materialization="materialized",
            )
        }
    )
    reverse = RelationshipTypeDefinition(
        id="relationship-type:event-of",
        predicate="event_of",
        business_name="event of",
        description="An event is associated with its subject.",
        source_type=forward.target_type,
        target_type=forward.source_type,
        publication_status="excluded",
    )
    contract = contract.model_copy(
        update={"relationship_types": [forward, reverse]}
    )
    approved = _approve(contract)
    paths = _write_bundle(tmp_path, approved)

    with pytest.raises(
        SemanticCompileError,
        match="Materialized inverse relationships must reference",
    ):
        compile_semantic_bundle(
            load_semantic_bundle(
                contract_path=paths[0],
                mappings_path=paths[1],
                vocabulary_path=paths[2],
                ids_lock_path=paths[3],
            )
        )


def test_graph_builder_uses_contract_owned_labels(tmp_path: Path) -> None:
    contract = _approve(_contract())
    contract_path, mappings_path, vocabulary_path, lock_path = _write_bundle(
        tmp_path, contract
    )
    compiled = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=contract_path,
            mappings_path=mappings_path,
            vocabulary_path=vocabulary_path,
            ids_lock_path=lock_path,
        )
    )
    parts = build_graph_model_parts(
        entity_types=list(compiled.graph_entity_types),
        relationship_pairs=list(compiled.graph_relationships),
        node_labels=compiled.graph_node_labels,
    )
    graph_type = next(
        part["payload_json"] for part in parts if part["path"] == "graphType.json"
    )
    assert {item["labels"][0] for item in graph_type["nodeTypes"]} == {
        "BusinessObject",
        "Subject",
        "Event",
    }
    assert graph_type["edgeTypes"][0]["labels"] == ["has_event"]


def test_shared_compiler_refuses_missing_fabric_type_id(tmp_path: Path) -> None:
    contract = _approve(_contract())
    ids = _ids(contract)
    ids.entity_types["Subject"] = StableIdBinding(
        semantic_id=contract.entity_types[1].id
    )
    contract_path, mappings_path, vocabulary_path, lock_path = _write_bundle(
        tmp_path, contract
    )
    lock_path.write_text(
        json.dumps(ids.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    bundle = load_semantic_bundle(
        contract_path=contract_path,
        mappings_path=mappings_path,
        vocabulary_path=vocabulary_path,
        ids_lock_path=lock_path,
    )
    with pytest.raises(SemanticCompileError, match="no preserved Fabric type ID"):
        compile_semantic_bundle(bundle)


def test_contract_agent_instructions_use_exact_labels_and_direction(
    tmp_path: Path,
) -> None:
    contract = _approve(_contract("legal-compliance"))
    contract_path, mappings_path, vocabulary_path, lock_path = _write_bundle(
        tmp_path, contract
    )
    compiled = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=contract_path,
            mappings_path=mappings_path,
            vocabulary_path=vocabulary_path,
            ids_lock_path=lock_path,
        )
    )
    instructions = build_contract_agent_instructions(
        compiled.agent_semantic_context,
        competency_questions=[
            "Which subjects have evidence-backed events?"
        ],
        domain_context="Synthetic compliance operations.",
    )
    assert len(instructions) < 4000
    assert compiled.contract_hash in instructions
    assert "Use Graph for exact relationship traversal" in instructions
    assert "Fabric Lakehouse semantic source" not in instructions
    assert "Mandatory Lakehouse relationship fallback" not in instructions
    assert "Surface" not in instructions


def test_shared_compiler_is_stable_when_contract_order_changes(
    tmp_path: Path,
) -> None:
    approved = _approve(_contract())
    reordered = approved.model_copy(
        update={
            "entity_types": list(reversed(approved.entity_types)),
            "relationship_types": list(reversed(approved.relationship_types)),
        }
    )
    first_input = tmp_path / "first-input"
    second_input = tmp_path / "second-input"
    first_input.mkdir()
    second_input.mkdir()
    first_paths = _write_bundle(first_input, approved)
    second_paths = _write_bundle(second_input, reordered)
    second_paths[3].write_bytes(first_paths[3].read_bytes())
    first = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=first_paths[0],
            mappings_path=first_paths[1],
            vocabulary_path=first_paths[2],
            ids_lock_path=first_paths[3],
        )
    )
    second = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=second_paths[0],
            mappings_path=second_paths[1],
            vocabulary_path=second_paths[2],
            ids_lock_path=second_paths[3],
        )
    )
    first_out = first.write(tmp_path / "first")
    second_out = second.write(tmp_path / "second")

    for relative_path in (
        "ontology/model.yaml",
        "ontology/ids.lock.json",
        "graph/semantic-plan.json",
        "graph/label-catalog.json",
        "agents/semantic-context.json",
        "semantic-model-manifest.json",
        "semantic-crosswalk.json",
        "materialization-plan.json",
        "model-quality-report.json",
        "dependency-graph.json",
        "semantic-manifest.json",
    ):
        assert (first_out / relative_path).read_bytes() == (
            second_out / relative_path
        ).read_bytes()


def test_optional_relationships_are_explicit_in_agent_routing(
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract.relationship_types[0].publication_status = "optional"
    approved = _approve(contract)
    paths = _write_bundle(tmp_path, approved)
    compiled = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=paths[0],
            mappings_path=paths[1],
            vocabulary_path=paths[2],
            ids_lock_path=paths[3],
        )
    )

    instructions = build_contract_agent_instructions(
        compiled.agent_semantic_context
    )

    assert "selected source elements" in instructions
    assert "Do not use Lakehouse" in instructions


def test_shared_compiler_records_observed_availability_without_schema_pruning(
    tmp_path: Path,
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    contract = _approve(_contract())
    paths = _write_bundle(tmp_path, contract)
    data_dir = tmp_path / "parquet"
    data_dir.mkdir()
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"entity_type": "Subject"},
                {"entity_type": "Event"},
            ]
        ),
        data_dir / "semantic_entities.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist([{"relationship_type": "has_event"}]),
        data_dir / "semantic_relationships.parquet",
    )

    compiled = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=paths[0],
            mappings_path=paths[1],
            vocabulary_path=paths[2],
            ids_lock_path=paths[3],
        ),
        data_version="run-123",
        data_dir=data_dir,
    )

    availability = {
        item.semantic_id: item
        for item in compiled.materialization_plan.data_availability
    }
    assert set(availability) == {
        entity.id for entity in contract.entity_types
    } | {
        relationship.id for relationship in contract.relationship_types
    }
    assert availability["entity-type:business-object"].observed_rows == 0
    assert availability["entity-type:business-object"].status == "sufficient"
    assert {
        entity.semantic_id
        for entity in compiled.semantic_model_manifest.entity_types
    } == {entity.id for entity in contract.entity_types}


def test_shared_compiler_allows_lineage_properties_and_shared_entity_id_fields(
    tmp_path: Path,
) -> None:
    contract = _contract()
    subject = contract.entity_types[1]
    subject.properties.append(
        PropertyDefinition(name="source_file_id", type="string")
    )
    subject.lineage_properties.append("source_file_id")
    contract = _approve(contract)
    mappings = _mappings(contract)
    subject_mapping = next(
        item
        for item in mappings.entity_types
        if item.semantic_id == subject.id
    )
    subject_mapping.type_filter_column = None
    subject_mapping.type_filter_value = None
    subject_mapping.property_columns["source_file_id"] = "source_file_id"

    contract_path = tmp_path / "contract.yaml"
    mappings_path = tmp_path / "mappings.yaml"
    vocabulary_path = tmp_path / "vocabulary.yaml"
    lock_path = tmp_path / "ids.lock.json"
    contract_path.write_text(
        yaml.safe_dump(contract.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    mappings_path.write_text(
        yaml.safe_dump(mappings.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    vocabulary_path.write_text(
        yaml.safe_dump(_vocabulary().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    lock_path.write_text(
        json.dumps(_ids(contract).model_dump(mode="json")),
        encoding="utf-8",
    )

    compiled = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=contract_path,
            mappings_path=mappings_path,
            vocabulary_path=vocabulary_path,
            ids_lock_path=lock_path,
        )
    )
    lineage_entry = next(
        item
        for item in compiled.semantic_crosswalk.property_entries
        if item.owner_type_id == subject.id
        and item.search_field_or_filter == "source_file_id"
    )
    assert lineage_entry.data_agent_element_id is None


def test_persisted_semantic_authority_rejects_crosswalk_tampering(
    tmp_path: Path,
) -> None:
    contract = _approve(_contract())
    paths = _write_bundle(tmp_path, contract)
    compiled_dir = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=paths[0],
            mappings_path=paths[1],
            vocabulary_path=paths[2],
            ids_lock_path=paths[3],
        )
    ).write(tmp_path / "semantic")
    crosswalk_path = compiled_dir / "semantic-crosswalk.json"
    crosswalk = json.loads(crosswalk_path.read_text(encoding="utf-8"))
    crosswalk["entity_type_entries"][0]["graph_label"] = "Tampered"
    crosswalk_path.write_text(
        json.dumps(crosswalk),
        encoding="utf-8",
    )
    integration_path = compiled_dir / "semantic-manifest.json"
    integration = json.loads(
        integration_path.read_text(encoding="utf-8")
    )
    for artifact in integration["artifacts"]:
        if artifact["path"] == "semantic-crosswalk.json":
            artifact["sha256"] = (
                "sha256:"
                + hashlib.sha256(crosswalk_path.read_bytes()).hexdigest()
            )
    integration_path.write_text(
        json.dumps(integration),
        encoding="utf-8",
    )

    with pytest.raises(
        SemanticCompileError,
        match="CROSSWALK_PHYSICAL_MAPPING_MISMATCH",
    ):
        load_semantic_model_artifacts(compiled_dir)


def test_dependency_graph_invalidates_all_model_owned_surfaces(
    tmp_path: Path,
) -> None:
    contract = _approve(_contract())
    paths = _write_bundle(tmp_path, contract)
    compiled = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=paths[0],
            mappings_path=paths[1],
            vocabulary_path=paths[2],
            ids_lock_path=paths[3],
        )
    )
    nodes = {
        node.artifact_id: node
        for node in compiled.dependency_graph.nodes
    }

    assert set(nodes["semantic-contract"].invalidates) >= {
        "semantic-model-manifest",
        "semantic-crosswalk",
        "materialization-plan",
        "ontology-projection",
        "graph-projection",
        "search-projection",
        "agent-schema",
    }
    assert set(nodes["agent-schema"].depends_on) >= {
        "semantic-model-manifest",
        "semantic-crosswalk",
        "ontology-projection",
        "graph-projection",
        "search-projection",
    }


def test_model_change_regenerates_every_dependent_projection_hash(
    tmp_path: Path,
) -> None:
    first_contract = _approve(_contract())
    changed_contract = _contract()
    changed_contract.entity_types[1].description = (
        "A revised approved business definition for the tracked subject."
    )
    changed_contract = _approve(changed_contract)
    first_root = tmp_path / "first"
    changed_root = tmp_path / "changed"
    first_root.mkdir()
    changed_root.mkdir()
    first_paths = _write_bundle(first_root, first_contract)
    changed_paths = _write_bundle(changed_root, changed_contract)
    changed_paths[3].write_bytes(first_paths[3].read_bytes())

    first = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=first_paths[0],
            mappings_path=first_paths[1],
            vocabulary_path=first_paths[2],
            ids_lock_path=first_paths[3],
        )
    )
    changed = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=changed_paths[0],
            mappings_path=changed_paths[1],
            vocabulary_path=changed_paths[2],
            ids_lock_path=changed_paths[3],
        )
    )
    first_hashes = {
        node.artifact_id: node.artifact_hash
        for node in first.dependency_graph.nodes
    }
    changed_hashes = {
        node.artifact_id: node.artifact_hash
        for node in changed.dependency_graph.nodes
    }

    for artifact_id in (
        "semantic-model-manifest",
        "semantic-crosswalk",
        "materialization-plan",
        "model-quality-report",
        "ontology-projection",
        "graph-projection",
        "search-projection",
        "agent-schema",
    ):
        assert first_hashes[artifact_id] != changed_hashes[artifact_id]


@pytest.mark.parametrize(
    "domain",
    ["supply-chain", "clinical-operations", "legal-compliance"],
)
def test_offline_semantic_graph_and_agent_compile_commands(
    tmp_path: Path,
    domain: str,
) -> None:
    contract = _approve(_contract(domain))
    contract_path, mappings_path, vocabulary_path, lock_path = _write_bundle(
        tmp_path, contract
    )
    common = [
        "--contract",
        str(contract_path),
        "--mappings",
        str(mappings_path),
        "--vocabulary",
        str(vocabulary_path),
        "--ids-lock",
        str(lock_path),
    ]
    build_out = tmp_path / "build"
    semantic_out = build_out / "semantic"
    semantic_result = CliRunner().invoke(
        compile_semantic_cmd,
        [
            *common,
            "--out",
            str(semantic_out),
            "--ontology-name",
            "kgv021_Ontology",
        ],
    )
    assert semantic_result.exit_code == 0, semantic_result.output
    assert (semantic_out / "semantic-manifest.json").exists()

    ontology_out = build_out / "ontology"
    ontology_result = CliRunner().invoke(
        cli,
        [
            "compile-ontology",
            "--semantic-dir",
            str(semantic_out),
            "--out",
            str(ontology_out),
        ],
    )
    assert ontology_result.exit_code == 0, ontology_result.output
    ontology_manifest = json.loads(
        (ontology_out / "ontology-manifest.json").read_text(encoding="utf-8")
    )
    assert ontology_manifest["contract_hash"] == contract.approval.contract_hash
    ontology_platform = json.loads(
        (ontology_out / ".platform").read_text(encoding="utf-8")
    )
    assert (
        ontology_platform["metadata"]["displayName"]
        == "kgv021_Ontology"
    )

    graph_out = build_out / "graph"
    graph_result = CliRunner().invoke(
        compile_graph_cmd,
        [
            "--semantic-dir",
            str(semantic_out),
            "--out",
            str(graph_out),
            "--workspace-id",
            "workspace",
            "--lakehouse-id",
            "lakehouse",
        ],
    )
    assert graph_result.exit_code == 0, graph_result.output
    graph_definition = json.loads(
        (graph_out / "graph-definition.json").read_text(encoding="utf-8")
    )
    assert graph_definition["parts"][1]["payload_json"]["edgeTypes"][0][
        "labels"
    ] == ["has_event"]
    assert graph_definition["parts"][1]["payload_json"]["edgeTypes"][0][
        "alias"
    ] == "has_event_edgeType"

    agent_out = build_out / "agents"
    competency_path = tmp_path / "competency.yaml"
    competency_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "cases": [
                    {
                        "id": "subjects-with-events",
                        "question": "Which subjects have events?",
                        "semantic_plan": {
                            "intent": "find_subject_events",
                            "requested_concepts": ["subject", "event"],
                            "required_types": [
                                f"entity-type:{domain}-subject",
                                f"entity-type:{domain}-event",
                            ],
                            "required_relationships": [
                                f"relationship-type:{domain}-has-event"
                            ],
                            "optional_relationships": [],
                            "requested_properties": [],
                            "evidence_required": True,
                            "path_steps": [
                                {
                                    "step_id": "subject-event",
                                    "from_type_id": (
                                        f"entity-type:{domain}-subject"
                                    ),
                                    "via_relationship_id": (
                                        f"relationship-type:{domain}-has-event"
                                    ),
                                    "to_type_id": (
                                        f"entity-type:{domain}-event"
                                    ),
                                    "direction": "source_to_target",
                                    "optional": False,
                                }
                            ],
                        },
                        "expected": {
                            "entity_types": [
                                f"entity-type:{domain}-subject",
                                f"entity-type:{domain}-event",
                            ],
                            "relationship_types": [
                                {
                                    "semantic_id": f"relationship-type:{domain}-has-event",
                                    "requirement": "required",
                                    "direction": "source_to_target",
                                }
                            ],
                            "answer_concepts": ["subject", "event"],
                            "evidence_required": True,
                        },
                        "routes": {
                            "direct_graph": "required",
                            "search": "required",
                            "data_agent_mcp": "required",
                        },
                        "probes": {
                            "direct_graph": {
                                "query": (
                                    "MATCH (s:Subject)-[r:has_event]->"
                                    "(e:Event) RETURN s.entity_id AS "
                                    "subject_id, e.entity_id AS event_id "
                                    "LIMIT 100"
                                ),
                                "entity_bindings": [
                                    {
                                        "column": "subject_id",
                                        "semantic_id": f"entity-type:{domain}-subject",
                                    },
                                    {
                                        "column": "event_id",
                                        "semantic_id": f"entity-type:{domain}-event",
                                    },
                                ],
                                "relationship_bindings": [
                                    {
                                        "semantic_id": (
                                            f"relationship-type:{domain}-has-event"
                                        ),
                                        "source_column": "subject_id",
                                        "target_column": "event_id",
                                        "direction": "source_to_target",
                                    }
                                ],
                                "canonical_id_columns": [
                                    "subject_id",
                                    "event_id",
                                ],
                            },
                            "search": {},
                            "data_agent_mcp": {},
                        },
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    agent_result = CliRunner().invoke(
        compile_agent_cmd,
        [
            "--semantic-dir",
            str(semantic_out),
            "--out",
            str(agent_out),
            "--question",
            "Which subjects have events?",
            "--competency-suite",
            str(competency_path),
        ],
    )
    assert agent_result.exit_code == 0, agent_result.output
    instructions = (agent_out / "instructions.md").read_text(encoding="utf-8")
    assert "Which subjects have events?" in instructions
    assert "has_event" not in instructions
    agent_manifest = json.loads(
        (agent_out / "agent-manifest.json").read_text(encoding="utf-8")
    )
    assert agent_manifest["contract_hash"] == contract.approval.contract_hash
    assert agent_manifest["instruction_hash"].startswith("sha256:")
    assert agent_manifest["competency_status"] == "compiled"
    assert agent_manifest["competency_contract_hash"].startswith("sha256:")
    assert agent_manifest["persisted_query_schema_hash"].startswith("sha256:")
    assert (agent_out / "persisted-query-schema.json").exists()

    validation_result = CliRunner().invoke(
        cli,
        [
            "validate-artifacts",
            "--build-dir",
            str(build_out),
            "--no-require-search",
            "--require-competency",
        ],
    )
    assert validation_result.exit_code == 0, validation_result.output

    compiled_contract = load_competency_contract(
        agent_out / "competency-contract.json"
    )
    assert compiled_contract.cases[0].probes.direct_graph is not None
    assert "has_event" in compiled_contract.cases[0].probes.direct_graph.query
    assert compiled_contract.query_schema is not None
    agent_manifest = json.loads(
        (agent_out / "agent-manifest.json").read_text(encoding="utf-8")
    )
    semantic_manifest = json.loads(
        (semantic_out / "semantic-model-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    semantic_hash = contract.approval.contract_hash
    competency_hash = agent_manifest["competency_contract_hash"]
    instruction_hash = agent_manifest["instruction_hash"]
    persisted_query_schema_hash = compiled_contract.query_schema.schema_hash
    assert persisted_query_schema_hash is not None
    expected = compiled_contract.cases[0].expected
    expected_relationships = [
        relationship.model_dump(mode="json")
        for relationship in expected.relationship_types
    ]
    canonical_ids = [
        f"entity:{domain}-subject-001",
        f"entity:{domain}-event-001",
    ]
    citation = {
        "citation_id": f"chunk:{domain}-001",
        "evidence_id": f"evidence:{domain}-001",
        "asset_version_id": f"version-{domain}-001",
        "blob_url": (
            "https://synthetic.blob.core.windows.net/kg-assets/raw/"
            f"asset-{domain}-001/versions/version-{domain}-001/"
            "original/source.txt"
        ),
    }
    config = RuntimeConfig.model_validate(
        {
            "environment": f"synthetic-{domain}",
            "contract_hash": competency_hash,
            "deployment": {
                "artifact_validation_status": "passed",
                "knowledge_http_status": 200,
                "partial_source": False,
                "data_agent_published": True,
                "compiled_instruction_hash": instruction_hash,
                "deployed_instruction_hash": instruction_hash,
                "receipt_path": f"{domain}-deployment-receipt.json",
                "receipt_sha256": "sha256:" + "1" * 64,
                "semantic_contract_hash": semantic_hash,
                "semantic_artifact_set_hash": "sha256:" + "2" * 64,
                "graph_artifact_set_hash": "sha256:" + "3" * 64,
                "search_artifact_set_hash": "sha256:" + "4" * 64,
                "semantic_model_manifest_hash": semantic_manifest[
                    "manifest_hash"
                ],
                "ontology_persisted_projection_hash": "sha256:" + "5" * 64,
                "graph_persisted_projection_hash": "sha256:" + "6" * 64,
                "receipt_instruction_hash": instruction_hash,
                "receipt_deployed_instruction_hash": instruction_hash,
                "persisted_query_schema_hash": persisted_query_schema_hash,
                "competency_contract_hash": competency_hash,
                "package_hash": "sha256:" + "7" * 64,
                "graph_model_id": f"graph-{domain}",
                "search_index_name": f"search-{domain}",
                "data_agent_id": f"agent-{domain}",
                "contract_hash_consistent": True,
            },
            "graph": {
                "workspace_id": f"workspace-{domain}",
                "graph_model_id": f"graph-{domain}",
            },
            "search": {
                "endpoint": "https://synthetic.search.windows.net",
                "index_name": f"search-{domain}",
            },
            "data_agent_mcp": {
                "endpoint": (
                    "https://api.fabric.microsoft.com/v1/mcp/workspaces/"
                    f"workspace-{domain}/dataagents/agent-{domain}/agent"
                ),
                "workspace_id": f"workspace-{domain}",
                "data_agent_id": f"agent-{domain}",
            },
        }
    )
    evidence = RuntimeEvidenceCollector(
        contract=compiled_contract,
        config=config,
        graph_executor=_StaticRuntimeExecutor(
            {
                "status": "success",
                "result_category": "success",
                "final_semantic_status": "success",
                "row_count": 1,
                "entity_types": list(expected.entity_types),
                "relationships": expected_relationships,
                "canonical_ids": canonical_ids,
                "accepted_relationships": [
                    {
                        "id": f"relationship:{domain}-001",
                        "evidence_ids": [citation["evidence_id"]],
                    }
                ],
                "physical_query_hash": "sha256:" + "8" * 64,
                "semantic_plan_hash": (
                    compiled_contract.cases[0].semantic_plan.plan_hash
                ),
                "static_validation_passed": True,
                "request_ids": [f"graph-request-{domain}"],
                "latency_ms": 1.0,
            }
        ),
        search_executor=_StaticRuntimeExecutor(
            {
                "status": "success",
                "result_category": "success",
                "final_semantic_status": "success",
                "http_status": 200,
                "partial_source": False,
                "result_count": 1,
                "canonical_ids": canonical_ids,
                "citations": [citation],
                "accepted_facts": [
                    {
                        "id": f"fact:{domain}-001",
                        "evidence_ids": [citation["evidence_id"]],
                    }
                ],
                "request_ids": [f"search-request-{domain}"],
                "latency_ms": 2.0,
            }
        ),
        mcp_executor=_StaticRuntimeExecutor(
            {
                "status": "success",
                "result_category": "success",
                "final_semantic_status": "success",
                "answer": (
                    "The subject has an evidence-backed event in the "
                    f"{domain} domain."
                ),
                "citations": [citation],
                "request_ids": [
                    f"mcp-request-{domain}",
                    f"mcp-correlation-{domain}",
                ],
                "client_request_ids": [f"mcp-operation-{domain}"],
                "retry_correlation_ids": [f"mcp-correlation-{domain}"],
                "retry_count": 0,
                "idempotency_key": f"turn-{domain}-001",
                "latency_ms": 3.0,
            }
        ),
    ).collect()
    deployment_validation = validate_deployment_evidence(evidence)
    evaluation = evaluate_runtime_evidence(evidence)
    report = build_runtime_report(
        evidence,
        deployment_validation=deployment_validation,
        evaluation=evaluation,
    )

    assert deployment_validation["status"] == "passed"
    assert evaluation["violations"] == [], evaluation["metrics"]
    assert evaluation["status"] == "passed", evaluation
    assert report["status"] == "passed"
    assert len(evidence["diagnostic_records"]) == 1

    diagnostics_path = tmp_path / f"{domain}-diagnostics.json"
    diagnostics_path.write_text(
        json.dumps(evidence["diagnostic_records"]),
        encoding="utf-8",
    )
    diagnostic_report = inspect_files([diagnostics_path])
    assert diagnostic_report["status"] == "complete"
    assert diagnostic_report["completeness"]["overall_coverage"] == 1.0


def test_graph_compile_omits_semantic_only_properties_for_base_tables(
    tmp_path: Path,
) -> None:
    contract = _approve(_contract("base-table-domain"))
    contract_path, mappings_path, vocabulary_path, lock_path = _write_bundle(
        tmp_path, contract
    )
    mappings = yaml.safe_load(mappings_path.read_text(encoding="utf-8"))
    for mapping in mappings["entity_types"]:
        mapping["table"] = "entities"
    for mapping in mappings["relationship_types"]:
        mapping["table"] = "relationships"
    mappings_path.write_text(
        yaml.safe_dump(mappings, sort_keys=False),
        encoding="utf-8",
    )
    graph_out = tmp_path / "graph"

    result = CliRunner().invoke(
        compile_graph_cmd,
        [
            "--contract",
            str(contract_path),
            "--mappings",
            str(mappings_path),
            "--vocabulary",
            str(vocabulary_path),
            "--ids-lock",
            str(lock_path),
            "--out",
            str(graph_out),
            "--workspace-id",
            "workspace",
            "--lakehouse-id",
            "lakehouse",
        ],
    )

    assert result.exit_code == 0, result.output
    definition = json.loads(
        (graph_out / "graph-definition.json").read_text(encoding="utf-8")
    )
    data_sources = definition["parts"][0]["payload_json"]["dataSources"]
    assert {source["properties"]["path"] for source in data_sources} == {
        "Tables/dbo/kg_entity_business_object",
        "Tables/dbo/kg_entity_base_table_domain_subject",
        "Tables/dbo/kg_entity_base_table_domain_event",
        "Tables/dbo/kg_relationship_base_table_domain_has_event",
    }
    graph_type = definition["parts"][1]["payload_json"]
    node_properties = {
        prop["name"] for prop in graph_type["nodeTypes"][0]["properties"]
    }
    edge_properties = {
        prop["name"] for prop in graph_type["edgeTypes"][0]["properties"]
    }
    assert "aliases_json" not in node_properties
    assert "assertion_status" not in edge_properties


def test_artifact_validation_rejects_graph_definition_drift(
    tmp_path: Path,
) -> None:
    contract = _approve(_contract("graph-drift"))
    paths = _write_bundle(tmp_path, contract)
    build = tmp_path / "build"
    semantic_dir = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=paths[0],
            mappings_path=paths[1],
            vocabulary_path=paths[2],
            ids_lock_path=paths[3],
        )
    ).write(build / "semantic")
    ontology_result = CliRunner().invoke(
        cli,
        [
            "compile-ontology",
            "--semantic-dir",
            str(semantic_dir),
            "--out",
            str(build / "ontology"),
        ],
    )
    assert ontology_result.exit_code == 0, ontology_result.output
    graph_result = CliRunner().invoke(
        compile_graph_cmd,
        [
            "--semantic-dir",
            str(semantic_dir),
            "--out",
            str(build / "graph"),
        ],
    )
    assert graph_result.exit_code == 0, graph_result.output
    agent_result = CliRunner().invoke(
        compile_agent_cmd,
        [
            "--semantic-dir",
            str(semantic_dir),
            "--out",
            str(build / "agents"),
        ],
    )
    assert agent_result.exit_code == 0, agent_result.output

    graph_definition_path = build / "graph" / "graph-definition.json"
    graph_definition = json.loads(
        graph_definition_path.read_text(encoding="utf-8")
    )
    graph_type = next(
        part["payload_json"]
        for part in graph_definition["parts"]
        if part["path"] == "graphType.json"
    )
    graph_type["nodeTypes"][0]["labels"] = ["Tampered"]
    graph_definition_path.write_text(
        json.dumps(graph_definition),
        encoding="utf-8",
    )
    graph_manifest_path = build / "graph" / "graph-manifest.json"
    graph_manifest = json.loads(
        graph_manifest_path.read_text(encoding="utf-8")
    )
    graph_manifest["artifacts"]["graph-definition.json"] = (
        "sha256:"
        + hashlib.sha256(graph_definition_path.read_bytes()).hexdigest()
    )
    graph_manifest_path.write_text(
        json.dumps(graph_manifest),
        encoding="utf-8",
    )

    with pytest.raises(SemanticArtifactValidationError) as exc_info:
        validate_compiled_semantic_artifacts(
            build,
            require_search=False,
            require_model_authority=True,
        )
    assert "GRAPH_ENTITY_PROJECTION_DRIFT" in {
        finding.code for finding in exc_info.value.findings
    }


def test_artifact_validation_rejects_query_schema_model_manifest_drift(
    tmp_path: Path,
) -> None:
    contract = _approve(_contract("query-schema-drift"))
    paths = _write_bundle(tmp_path, contract)
    build = tmp_path / "build"
    semantic_dir = compile_semantic_bundle(
        load_semantic_bundle(
            contract_path=paths[0],
            mappings_path=paths[1],
            vocabulary_path=paths[2],
            ids_lock_path=paths[3],
        )
    ).write(build / "semantic")
    for command, out_name in (
        ("compile-ontology", "ontology"),
        ("compile-graph", "graph"),
        ("compile-agent", "agents"),
    ):
        result = CliRunner().invoke(
            cli,
            [
                command,
                "--semantic-dir",
                str(semantic_dir),
                "--out",
                str(build / out_name),
            ],
        )
        assert result.exit_code == 0, result.output

    query_schema_path = build / "agents" / "persisted-query-schema.json"
    query_schema = PersistedQuerySchema.model_validate_json(
        query_schema_path.read_text(encoding="utf-8")
    ).model_copy(
        update={
            "manifest_hash": "sha256:" + "f" * 64,
            "schema_hash": "",
        }
    )
    query_schema = query_schema.model_copy(
        update={
            "schema_hash": compute_persisted_query_schema_hash(query_schema)
        }
    )
    query_schema_path.write_text(
        json.dumps(query_schema.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    agent_manifest_path = build / "agents" / "agent-manifest.json"
    agent_manifest = json.loads(
        agent_manifest_path.read_text(encoding="utf-8")
    )
    agent_manifest["persisted_query_schema_hash"] = query_schema.schema_hash
    agent_manifest_path.write_text(
        json.dumps(agent_manifest, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(SemanticArtifactValidationError) as exc_info:
        validate_compiled_semantic_artifacts(
            build,
            require_search=False,
            require_model_authority=True,
        )
    assert "QUERY_SCHEMA_MODEL_MANIFEST_DRIFT" in {
        finding.code for finding in exc_info.value.findings
    }
