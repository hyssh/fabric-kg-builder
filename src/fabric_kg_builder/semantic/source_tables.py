"""Canonical/semantic Parquet source resolution shared by compile and deploy."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from fabric_kg_builder.contracts.base import (
    canonical_json,
    canonical_sha256,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.extraction import (
    RequiredMemberManifestV1_1,
    RequiredMemberOrderingPolicyV1_1,
    RequiredMemberReferenceV1_1,
)
from fabric_kg_builder.contracts.lifecycle import AssertionState
from fabric_kg_builder.contracts.projection import (
    AuditProjection,
    SemanticServingProjection,
    validate_asserted_serving_subset,
)
from fabric_kg_builder.contracts.publication import (
    ProjectionEquivalence,
    ProjectionEvidence,
)
from fabric_kg_builder.contracts.receipts import ArtifactManifest, StageReceipt
from fabric_kg_builder.contracts.resources import (
    StageResourceMetrics,
    validate_receipt_resources,
)
from fabric_kg_builder.model.arrow_schemas import L4_PROJECTION_TABLE_SCHEMAS

SOURCE_TABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "entities": ("entities", "semantic_entities"),
    "semantic_entities": ("semantic_entities", "entities"),
    "relationships": ("relationships", "semantic_relationships"),
    "semantic_relationships": ("semantic_relationships", "relationships"),
}


def source_table_candidates(source_table_name: str) -> tuple[str, ...]:
    """Return exact-first compatible source names for one semantic table."""
    table_name = source_table_name.removesuffix(".parquet")
    if not table_name or Path(table_name).name != table_name:
        raise ValueError(f"Unsafe source table name: {source_table_name!r}.")
    return SOURCE_TABLE_ALIASES.get(table_name, (table_name,))


def resolve_semantic_source_parquet(
    parquet_dir: Path | str,
    source_table_name: str,
) -> Path:
    """Resolve exact mapping name, then its canonical/semantic counterpart."""
    root = Path(parquet_dir)
    candidates = source_table_candidates(source_table_name)
    for candidate in candidates:
        path = root / f"{candidate}.parquet"
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"No source Parquet found for '{source_table_name}'. Tried "
        f"{[f'{candidate}.parquet' for candidate in candidates]} "
        f"under '{root}'."
    )


_SEALED_L4_TABLES = frozenset({
    "semantic_asserted_entities",
    "semantic_entity_type_assertions",
    "semantic_asserted_relationships",
    "semantic_asserted_properties",
    "semantic_required_member_manifests",
    "semantic_required_members",
})
_L4_PROJECTION_FILES = {
    "l4-audit-projection": (
        "audit-projection.json",
        "c0.audit_projection",
        canonical_sha256(AuditProjection.model_json_schema()),
    ),
    "l4-semantic-serving-projection": (
        "semantic-serving-projection.json",
        "c0.semantic_serving_projection",
        canonical_sha256(SemanticServingProjection.model_json_schema()),
    ),
    "l4-parquet-projection-equivalence": (
        "projection-equivalence.json",
        "c0.projection_equivalence",
        canonical_sha256({
            "type": "array",
            "items": ProjectionEquivalence.model_json_schema(),
        }),
    ),
}
_L4_STAGE_FILES = {
    "output-manifest.json",
    "resource-metrics.json",
    "stage-receipt.json",
}
L4_PROJECTION_CODE_VERSION = "l4-projection/1.0.0"
L4_ACCEPTED_VERSIONS = {
    "c0.artifact_manifest": "1.0.0",
    "c0.candidate_accounting_disposition": "1.0.0",
    "c0.candidate_lifecycle_record": "1.0.0",
    "c0.evidence_span": "1.1.0",
    "c0.extraction_candidate_batch": "1.0.0",
    "c0.required_member_manifest": "1.1.0",
    "c0.required_member_set_proposal": "1.1.0",
    "c0.source_unit": "1.0.0",
    "c0.stage_receipt": "1.0.0",
    "c0.stage_resource_metrics": "1.0.0",
    "c0.audit_projection": "1.0.0",
    "c0.projection_equivalence": "1.0.0",
    "c0.semantic_serving_projection": "1.0.0",
    "domain.contract": "2.0.0",
    "l1.design_sample_manifest": "1.0.0",
    "l1.source_corpus_manifest": "1.0.0",
    "l2.proposed_candidate_partition": "1.0.0",
    "l2.required_member_set_view": "1.1.0",
    "l3.classification_assertion": "1.0.0",
    "l3.property_observation": "1.0.0",
    "l4.projection_code": L4_PROJECTION_CODE_VERSION,
}


def _schema_hash(table_name: str) -> str:
    return canonical_sha256([
        {
            "name": field.name,
            "type": str(field.type),
            "nullable": field.nullable,
        }
        for field in L4_PROJECTION_TABLE_SCHEMAS[table_name]
    ])


def _one_artifact(manifest: ArtifactManifest, artifact_id: str):
    matches = [entry for entry in manifest.entries if entry.artifact_id == artifact_id]
    if len(matches) != 1:
        raise ValueError(f"L4 manifest does not bind exactly one {artifact_id}")
    return matches[0]


def _table_canonical_id_set_hash(
    table_name: str,
    rows: tuple[dict[str, object], ...],
) -> str:
    id_columns = {
        "audit_candidates": ("input_candidate_id",),
        "semantic_asserted_entities": ("entity_id",),
        "semantic_entity_type_assertions": (
            "entity_id",
            "semantic_type_id",
        ),
        "semantic_asserted_relationships": ("relationship_id",),
        "semantic_asserted_properties": ("property_assertion_id",),
        "semantic_required_member_manifests": (
            "required_member_manifest_id",
        ),
        "semantic_required_members": (
            "required_member_manifest_id",
            "member_canonical_id",
        ),
    }[table_name]
    return canonical_sha256(
        sorted({
            "|".join(str(row[column]) for column in id_columns)
            for row in rows
        })
    )


def _projection_table_hashes(
    rows: tuple[dict[str, object], ...],
    *,
    id_field: str,
) -> tuple[str, str, tuple[str, ...]]:
    ids = tuple(sorted({str(row[id_field]) for row in rows}))
    return canonical_sha256(ids), canonical_sha256(rows), ids


def _identity_lineage(value: object) -> dict[str, object]:
    return value.model_dump(mode="json", exclude={"contract_kind"})


@dataclass(frozen=True)
class SealedL4ServingSource:
    """Exact schema-2 source gate; raw canonical aliases are never accepted."""

    root: Path
    projection: SemanticServingProjection
    receipt: StageReceipt
    manifest: ArtifactManifest
    input_manifest: ArtifactManifest

    def __post_init__(self) -> None:
        try:
            canonical_input_manifest = ArtifactManifest.model_validate(
                self.input_manifest.model_dump(mode="python")
            )
        except ValueError as exc:
            raise ValueError(
                "schema-2 serving requires a canonical L3 input manifest"
            ) from exc
        if (
            canonical_input_manifest != self.input_manifest
            or self.input_manifest.artifact_manifest_id
            != self.receipt.input_manifest_id
            or self.input_manifest.manifest_hash
            != self.receipt.input_manifest_hash
        ):
            raise ValueError(
                "L4 receipt input manifest differs from supplied L3 authority"
            )
        if (
            self.receipt.stage_id != "L4"
            or self.receipt.stage_name != "schema2-audit-serving-projection"
            or self.receipt.stage_contract_version != "1.0.0"
            or self.receipt.status != "succeeded"
            or dict(self.receipt.accepted_contract_versions)
            != L4_ACCEPTED_VERSIONS
        ):
            raise ValueError("schema-2 serving requires a successful L4 receipt")
        if (
            self.receipt.output_manifest_id != self.manifest.artifact_manifest_id
            or self.receipt.output_manifest_hash != self.manifest.manifest_hash
            or self.manifest.artifact_manifest_id
            != deterministic_contract_id(
                "artifact-manifest",
                {"stage": "L4", "fingerprint": self.receipt.skip_key},
            )
            or _identity_lineage(self.manifest.identity)
            != _identity_lineage(self.receipt.identity)
            or self.receipt.input_manifest_id
            not in self.receipt.identity.parent_artifact_ids
        ):
            raise ValueError("L4 serving receipt and artifact manifest differ")
        if self.projection.source_manifest_hash != self.receipt.input_manifest_hash:
            raise ValueError("serving projection is not bound to the L4 input manifest")
        if self.projection.artifact_manifest_id != self.receipt.input_manifest_id:
            raise ValueError("serving projection references a different L3 manifest")
        if self.projection.identity.contract_kind != "c0.semantic_serving_projection":
            raise ValueError("schema-2 source is not a sealed serving projection")
        self._validate_stage_files()
        self._validate_complete_artifact_set()

    def _validate_stage_files(self) -> None:
        try:
            manifest_payload = (self.root / "output-manifest.json").read_bytes()
            receipt_payload = (self.root / "stage-receipt.json").read_bytes()
            metrics = StageResourceMetrics.model_validate_json(
                (self.root / "resource-metrics.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ValueError("sealed L4 stage metadata is missing or invalid") from exc
        if (
            manifest_payload
            != (canonical_json(self.manifest) + "\n").encode("utf-8")
            or receipt_payload
            != (canonical_json(self.receipt) + "\n").encode("utf-8")
        ):
            raise ValueError("sealed L4 stage metadata differs from supplied seals")
        try:
            validate_receipt_resources(self.receipt, metrics)
        except ValueError as exc:
            raise ValueError("sealed L4 metrics differ from the stage receipt") from exc
        expected_receipt_id = deterministic_contract_id(
            "stage-receipt",
            {"stage": "L4", "fingerprint": self.receipt.skip_key},
        )
        expected_metrics_id = deterministic_contract_id(
            "stage-resource-metrics",
            {"stage": "L4", "fingerprint": self.receipt.skip_key},
        )
        if (
            self.receipt.stage_receipt_id != expected_receipt_id
            or self.receipt.resource_metrics_id != expected_metrics_id
            or self.receipt.attempt_count != 1
            or self.receipt.remote_operation_refs
            or self.receipt.error_codes
            or metrics.resource_metrics_id != expected_metrics_id
            or metrics.stage_id != "L4"
            or metrics.stage_name != "schema2-audit-serving-projection"
            or _identity_lineage(metrics.identity)
            != _identity_lineage(self.receipt.identity)
            or metrics.storage_write_bytes
            != self.manifest.total_byte_count + len(manifest_payload)
            or metrics.network_request_bytes
            or metrics.network_response_bytes
            or metrics.source_units_read
            or metrics.source_units_written
            or metrics.source_units_skipped
            or metrics.document_intelligence_calls
            or metrics.document_intelligence_pages
            or metrics.foundry_calls
            or metrics.foundry_input_tokens
            or metrics.foundry_output_tokens
            or metrics.embedding_calls
            or metrics.embedding_items
            or metrics.fabric_calls
            or metrics.fabric_rows_read
            or metrics.fabric_rows_written
            or metrics.search_calls
            or metrics.search_documents_read
            or metrics.search_documents_written
            or metrics.retry_count
            or metrics.retry_wait_ms
            or metrics.cache_hits
            or metrics.cache_misses
            or metrics.max_observed_concurrency != 1
            or metrics.exceeded_dimensions
        ):
            raise ValueError("sealed L4 stage lineage or local metrics differ")

    def _validate_complete_artifact_set(self) -> None:
        expected_files = set(_L4_STAGE_FILES)
        expected_artifact_ids = {
            *(
                f"l4-table:{table_name}"
                for table_name in L4_PROJECTION_TABLE_SCHEMAS
            ),
            *_L4_PROJECTION_FILES,
        }
        if {entry.artifact_id for entry in self.manifest.entries} != (
            expected_artifact_ids
        ):
            raise ValueError("L4 manifest has missing or extra artifacts")
        table_rows: dict[str, tuple[dict[str, object], ...]] = {}
        for table_name in L4_PROJECTION_TABLE_SCHEMAS:
            expected_files.add(f"{table_name}.parquet")
            _, table_rows[table_name] = self._validate_table_artifact(table_name)
        audit: AuditProjection | None = None
        serving: SemanticServingProjection | None = None
        equivalences: tuple[ProjectionEquivalence, ...] | None = None
        for artifact_id, (file_name, contract_kind, schema_hash) in (
            _L4_PROJECTION_FILES.items()
        ):
            expected_files.add(file_name)
            entry = _one_artifact(self.manifest, artifact_id)
            path = self.root / file_name
            try:
                payload = path.read_bytes()
                raw = json.loads(payload)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"sealed L4 projection artifact is invalid: {path}"
                ) from exc
            if artifact_id == "l4-audit-projection":
                value = AuditProjection.model_validate_json(payload)
                audit = value
                expected_payload = (
                    canonical_json(value) + "\n"
                ).encode("utf-8")
                row_count = 1
            elif artifact_id == "l4-semantic-serving-projection":
                value = SemanticServingProjection.model_validate_json(payload)
                serving = value
                if value != self.projection:
                    raise ValueError(
                        "sealed serving projection differs from supplied projection"
                    )
                expected_payload = (
                    canonical_json(value) + "\n"
                ).encode("utf-8")
                row_count = 1
            else:
                values = tuple(
                    ProjectionEquivalence.model_validate_json(
                        json.dumps(item)
                    )
                    for item in raw
                )
                equivalences = values
                expected_payload = (
                    canonical_json(values) + "\n"
                ).encode("utf-8")
                row_count = len(values)
            if (
                entry.contract_kind != contract_kind
                or entry.contract_version != "1.0.0"
                or entry.schema_hash != schema_hash
                or entry.content_hash != hashlib.sha256(payload).hexdigest()
                or entry.byte_count != len(payload)
                or entry.row_count != row_count
                or entry.canonical_id_set_hash is not None
                or payload != expected_payload
            ):
                raise ValueError(
                    f"sealed L4 projection differs from its manifest: {path}"
                )
        if audit is None or serving is None or equivalences is None:
            raise ValueError("sealed L4 projections are incomplete")
        self._validate_cross_artifact_invariants(
            audit,
            serving,
            equivalences,
            table_rows,
        )
        actual_files = {
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file()
        }
        if actual_files != expected_files:
            raise ValueError("sealed L4 run has missing or extra files")

    def _validate_cross_artifact_invariants(
        self,
        audit: AuditProjection,
        serving: SemanticServingProjection,
        equivalences: tuple[ProjectionEquivalence, ...],
        tables: dict[str, tuple[dict[str, object], ...]],
    ) -> None:
        expected_audit_id = deterministic_contract_id(
            "audit-projection",
            {"fingerprint": self.receipt.skip_key},
        )
        expected_serving_id = deterministic_contract_id(
            "semantic-serving-projection",
            {"fingerprint": self.receipt.skip_key},
        )
        if (
            serving != self.projection
            or audit.projection_id != expected_audit_id
            or serving.projection_id != expected_serving_id
            or audit.source_manifest_hash != self.receipt.input_manifest_hash
            or serving.source_manifest_hash != self.receipt.input_manifest_hash
            or audit.artifact_manifest_id != self.receipt.input_manifest_id
            or serving.artifact_manifest_id != self.receipt.input_manifest_id
            or _identity_lineage(audit.identity)
            != _identity_lineage(self.receipt.identity)
            or _identity_lineage(serving.identity)
            != _identity_lineage(self.receipt.identity)
        ):
            raise ValueError("L4 projection identities or source lineage differ")

        for table_name, rows in tables.items():
            for row in rows:
                row_without_hash = {
                    key: value for key, value in row.items() if key != "row_hash"
                }
                if row.get("row_hash") != canonical_sha256(row_without_hash):
                    raise ValueError(
                        f"sealed L4 row hash differs in {table_name}"
                    )

        entity_rows = tables["semantic_asserted_entities"]
        relationship_rows = tables["semantic_asserted_relationships"]
        property_rows = tables["semantic_asserted_properties"]
        serving_specs = {
            "entity": (entity_rows, "entity_id"),
            "relationship": (relationship_rows, "relationship_id"),
            "property": (property_rows, "property_assertion_id"),
        }
        serving_ids: dict[str, tuple[str, ...]] = {}
        expected_serving_id_hashes: dict[str, str] = {}
        expected_serving_row_hashes: dict[str, str] = {}
        for kind, (rows, id_field) in serving_specs.items():
            id_hash, row_hash, ids = _projection_table_hashes(
                rows,
                id_field=id_field,
            )
            serving_ids[kind] = ids
            expected_serving_id_hashes[kind] = id_hash
            expected_serving_row_hashes[kind] = row_hash
            if len(rows) != len(ids):
                raise ValueError(
                    f"persisted serving {kind} IDs are not unique"
                )
        evidence_ids = tuple(sorted({
            str(evidence_id)
            for rows, _ in serving_specs.values()
            for row in rows
            for evidence_id in row["evidence_span_ids"]
        }))
        if (
            dict(serving.canonical_id_set_hashes)
            != expected_serving_id_hashes
            or dict(serving.canonical_row_hashes)
            != expected_serving_row_hashes
            or tuple(serving.entity_assertion_ids) != serving_ids["entity"]
            or tuple(serving.relationship_assertion_ids)
            != serving_ids["relationship"]
            or tuple(serving.property_assertion_ids) != serving_ids["property"]
            or tuple(serving.evidence_span_ids) != evidence_ids
        ):
            raise ValueError(
                "serving projection does not equal persisted asserted tables"
            )

        entity_ids = set(serving_ids["entity"])
        entity_by_id = {
            str(row["entity_id"]): row for row in entity_rows
        }
        type_rows_by_entity: defaultdict[str, list[dict[str, object]]] = (
            defaultdict(list)
        )
        type_keys: list[tuple[str, str]] = []
        depth_by_type: dict[str, int] = {}
        for row in tables["semantic_entity_type_assertions"]:
            type_rows_by_entity[str(row["entity_id"])].append(row)
            type_id = str(row["semantic_type_id"])
            type_keys.append((str(row["entity_id"]), type_id))
            depth = int(row["hierarchy_depth"])
            if type_id in depth_by_type and depth_by_type[type_id] != depth:
                raise ValueError("semantic type hierarchy depths are inconsistent")
            depth_by_type[type_id] = depth
        if len(type_keys) != len(set(type_keys)):
            raise ValueError("entity type assertion IDs are not unique")
        for row in entity_rows:
            entity_id = str(row["entity_id"])
            type_rows = type_rows_by_entity.pop(entity_id, [])
            asserted_type_ids = [
                str(value) for value in row["asserted_type_ids"]
            ]
            most_specific = str(row["most_specific_type_id"])
            most_specific_depth = depth_by_type.get(most_specific)
            if (
                len(asserted_type_ids) != len(set(asserted_type_ids))
                or set(asserted_type_ids)
                != {str(item["semantic_type_id"]) for item in type_rows}
                or len(type_rows) != len(asserted_type_ids)
                or most_specific not in asserted_type_ids
                or most_specific_depth is None
                or any(
                    str(item["most_specific_type_id"])
                    != most_specific
                    or bool(item["is_most_specific"])
                    != (str(item["semantic_type_id"]) == most_specific)
                    or item["hierarchy_hash"] != row["hierarchy_hash"]
                    or item["identity_policy_hash"]
                    != row["identity_policy_hash"]
                    for item in type_rows
                )
                or any(
                    int(item["hierarchy_depth"]) >= most_specific_depth
                    for item in type_rows
                    if str(item["semantic_type_id"]) != most_specific
                )
            ):
                raise ValueError(
                    f"entity type assertions differ for {entity_id}"
                )
        if type_rows_by_entity or any(
            row["source_entity_id"] not in entity_ids
            or row["target_entity_id"] not in entity_ids
            for row in relationship_rows
        ):
            raise ValueError("serving type or relationship endpoints are unresolved")
        hierarchy_hashes = {
            str(row["hierarchy_hash"]) for row in entity_rows
        } | {
            str(row["hierarchy_hash"])
            for row in tables["semantic_entity_type_assertions"]
        } | {
            str(row["hierarchy_hash"]) for row in relationship_rows
        } | {
            str(row["hierarchy_hash"])
            for row in tables["semantic_required_member_manifests"]
        } | {
            str(row["hierarchy_hash"])
            for row in tables["semantic_required_members"]
        }
        if len(hierarchy_hashes) > 1 or any(
            row["hierarchy_hash"]
            != entity_by_id[str(row["source_entity_id"])]["hierarchy_hash"]
            or row["hierarchy_hash"]
            != entity_by_id[str(row["target_entity_id"])]["hierarchy_hash"]
            for row in relationship_rows
        ):
            raise ValueError("serving hierarchy authority differs across tables")
        identity_policy_hashes = {
            str(row["identity_policy_hash"]) for row in entity_rows
        } | {
            str(row["identity_policy_hash"])
            for row in tables["semantic_entity_type_assertions"]
        } | {
            str(row["identity_policy_hash"])
            for row in tables["semantic_required_member_manifests"]
        } | {
            str(row["identity_policy_hash"])
            for row in tables["semantic_required_members"]
        }
        if len(identity_policy_hashes) > 1:
            raise ValueError(
                "serving identity-policy authority differs across tables"
            )
        for rows in (entity_rows, relationship_rows, property_rows):
            if any(
                row["domain_contract_hash"]
                != serving.sealed_domain_contract_hash
                or row["semantic_contract_hash"]
                != serving.sealed_semantic_contract_hash
                for row in rows
            ):
                raise ValueError("serving table contract authority differs")

        audit_rows = tables["audit_candidates"]
        dispositions_by_id = {
            item.input_candidate_id: item for item in audit.candidate_dispositions
        }
        persisted_input_ids = [
            str(row["input_candidate_id"]) for row in audit_rows
        ]
        if (
            len(persisted_input_ids) != len(set(persisted_input_ids))
            or set(persisted_input_ids) != set(dispositions_by_id)
        ):
            raise ValueError(
                "audit rows do not exactly cover candidate dispositions"
            )
        audit_ids: dict[str, tuple[str, ...]] = {}
        asserted_audit_ids: dict[str, set[str]] = {}
        expected_audit_id_hashes: dict[str, str] = {}
        expected_audit_row_hashes: dict[str, str] = {}
        for kind in ("entity", "relationship", "property"):
            ids = tuple(sorted({
                str(row["semantic_assertion_id"])
                for row in audit_rows
                if row["candidate_kind"] == kind
            }))
            kind_rows = tuple(
                row for row in audit_rows if row["candidate_kind"] == kind
            )
            audit_ids[kind] = ids
            asserted_audit_ids[kind] = {
                str(row["semantic_assertion_id"])
                for row in audit_rows
                if row["candidate_kind"] == kind
                and row["disposition"] == "retained"
                and row["lifecycle_state"] == AssertionState.ASSERTED.value
            }
            expected_audit_id_hashes[kind] = canonical_sha256(ids)
            expected_audit_row_hashes[kind] = (
                expected_serving_row_hashes[kind]
                if ids == serving_ids[kind]
                else canonical_sha256(kind_rows)
            )
        states: Counter[AssertionState] = Counter()
        reasons: Counter[str] = Counter()
        retained_by_candidate = {
            str(row["candidate_id"]): row
            for row in audit_rows
            if row["disposition"] == "retained"
        }
        deduplicated_lineage_fields = (
            "candidate_kind",
            "semantic_assertion_id",
            "approved_semantic_id",
            "evidence_span_ids",
            "resolved_source_entity_id",
            "resolved_target_entity_id",
            "source_inheritance_path",
            "target_inheritance_path",
            "source_manifest_hash",
        )
        for row in audit_rows:
            disposition = dispositions_by_id.get(str(row["input_candidate_id"]))
            state = (
                AssertionState(str(row["lifecycle_state"]))
                if row["lifecycle_state"] is not None
                else None
            )
            if (
                disposition is None
                or row["disposition"] != disposition.disposition
                or row["retained_candidate_id"]
                != disposition.retained_candidate_id
                or row["deduplicated_into_candidate_id"]
                != disposition.deduplicated_into_candidate_id
                or state != disposition.current_state
                or tuple(row["reason_codes"]) != tuple(disposition.reason_codes)
                or row["source_manifest_hash"]
                != self.receipt.input_manifest_hash
            ):
                raise ValueError("audit projection and persisted accounting differ")
            target_id = (
                disposition.retained_candidate_id
                if disposition.disposition == "retained"
                else disposition.deduplicated_into_candidate_id
            )
            retained_row = retained_by_candidate.get(str(target_id))
            if (
                row["candidate_id"] != target_id
                or retained_row is None
                or (
                    disposition.disposition == "deduplicated"
                    and any(
                        row[field] != retained_row[field]
                        for field in deduplicated_lineage_fields
                    )
                )
            ):
                raise ValueError(
                    "audit row lineage differs from its disposition target"
                )
            if state is not None:
                states[state] += 1
            reasons.update(str(reason) for reason in row["reason_codes"])
        if (
            len(dispositions_by_id) != len(audit_rows)
            or audit.input_candidate_count != len(audit_rows)
            or audit.retained_candidate_count != sum(states.values())
            or audit.deduplicated_input_count
            != sum(row["disposition"] == "deduplicated" for row in audit_rows)
            or dict(audit.lifecycle_state_counts)
            != {state: states.get(state, 0) for state in AssertionState}
            or dict(audit.reason_code_counts) != dict(sorted(reasons.items()))
            or tuple(audit.entity_assertion_ids) != audit_ids["entity"]
            or tuple(audit.relationship_assertion_ids)
            != audit_ids["relationship"]
            or tuple(audit.property_assertion_ids) != audit_ids["property"]
            or dict(audit.canonical_id_set_hashes)
            != expected_audit_id_hashes
            or dict(audit.canonical_row_hashes)
            != expected_audit_row_hashes
        ):
            raise ValueError("audit projection does not equal persisted audit rows")
        try:
            validate_asserted_serving_subset(
                audit,
                serving,
                asserted_entity_ids=asserted_audit_ids["entity"],
                asserted_relationship_ids=asserted_audit_ids["relationship"],
                asserted_property_ids=asserted_audit_ids["property"],
            )
        except ValueError as exc:
            raise ValueError("audit and serving projections are inconsistent") from exc
        self._validate_serving_lineage(
            audit_rows,
            entity_rows,
            relationship_rows,
            property_rows,
        )

        self._validate_projection_equivalences(
            serving,
            equivalences,
            tables["semantic_required_member_manifests"],
            tables["semantic_required_members"],
        )

    def _validate_serving_lineage(
        self,
        audit_rows: tuple[dict[str, object], ...],
        entity_rows: tuple[dict[str, object], ...],
        relationship_rows: tuple[dict[str, object], ...],
        property_rows: tuple[dict[str, object], ...],
    ) -> None:
        asserted: defaultdict[
            tuple[str, str],
            list[dict[str, object]],
        ] = defaultdict(list)
        for row in audit_rows:
            if (
                row["disposition"] == "retained"
                and row["lifecycle_state"] == AssertionState.ASSERTED.value
            ):
                asserted[(
                    str(row["candidate_kind"]),
                    str(row["semantic_assertion_id"]),
                )].append(row)

        def common(
            kind: str,
            assertion_id: str,
            row: dict[str, object],
        ) -> list[dict[str, object]]:
            group = asserted.pop((kind, assertion_id), [])
            if (
                list(row["candidate_ids"])
                != sorted(str(item["candidate_id"]) for item in group)
                or list(row["evidence_span_ids"])
                != sorted({
                    str(evidence_id)
                    for item in group
                    for evidence_id in item["evidence_span_ids"]
                })
            ):
                raise ValueError(
                    f"serving {kind} lineage differs for {assertion_id}"
                )
            return group

        for row in entity_rows:
            entity_id = str(row["entity_id"])
            group = common("entity", entity_id, row)
            approved_ids = {
                str(item["approved_semantic_id"]) for item in group
            }
            if (
                not approved_ids
                or "None" in approved_ids
                or not approved_ids.issubset(
                    str(value) for value in row["asserted_type_ids"]
                )
                or str(row["most_specific_type_id"]) not in approved_ids
            ):
                raise ValueError(
                    f"serving entity classification differs for {entity_id}"
                )

        for row in relationship_rows:
            relationship_id = str(row["relationship_id"])
            group = common("relationship", relationship_id, row)
            if (
                {item["approved_semantic_id"] for item in group}
                != {row["semantic_relationship_id"]}
                or {item["resolved_source_entity_id"] for item in group}
                != {row["source_entity_id"]}
                or {item["resolved_target_entity_id"] for item in group}
                != {row["target_entity_id"]}
                or {
                    tuple(item["source_inheritance_path"]) for item in group
                }
                != {tuple(row["source_inheritance_path"])}
                or {
                    tuple(item["target_inheritance_path"]) for item in group
                }
                != {tuple(row["target_inheritance_path"])}
            ):
                raise ValueError(
                    f"serving relationship lineage differs for {relationship_id}"
                )

        for row in property_rows:
            property_id = str(row["property_assertion_id"])
            group = common("property", property_id, row)
            if {item["approved_semantic_id"] for item in group} != {
                row["semantic_property_id"]
            }:
                raise ValueError(
                    f"serving property lineage differs for {property_id}"
                )
        if asserted:
            raise ValueError("asserted audit lineage is missing from serving tables")

    def _validate_projection_equivalences(
        self,
        serving: SemanticServingProjection,
        equivalences: tuple[ProjectionEquivalence, ...],
        manifest_rows: tuple[dict[str, object], ...],
        member_rows: tuple[dict[str, object], ...],
    ) -> None:
        manifest_by_id = {
            str(row["required_member_manifest_id"]): row
            for row in manifest_rows
        }
        members_by_manifest: defaultdict[str, list[dict[str, object]]] = (
            defaultdict(list)
        )
        for row in member_rows:
            members_by_manifest[str(row["required_member_manifest_id"])].append(
                row
            )
        proof_by_manifest = {
            proof.authority.required_member_manifest_id: proof
            for proof in equivalences
        }
        if (
            len(manifest_by_id) != len(manifest_rows)
            or len(proof_by_manifest) != len(equivalences)
            or set(proof_by_manifest) != set(manifest_by_id)
        ):
            raise ValueError("required-member equivalence proof set is incomplete")
        source_entries = tuple(
            entry
            for entry in self.input_manifest.entries
            if entry.contract_kind == "c0.required_member_manifest"
        )
        source_entry_by_id = {
            entry.artifact_id: entry for entry in source_entries
        }
        if (
            len(source_entry_by_id) != len(source_entries)
            or set(source_entry_by_id) != set(manifest_by_id)
        ):
            raise ValueError(
                "projected required-member authority does not exactly match "
                "the anchored L3 manifest"
            )
        schema_hash = canonical_sha256(
            RequiredMemberManifestV1_1.model_json_schema()
        )
        common_fields = (
            "required_member_set_proposal_id",
            "required_member_set_proposal_hash",
            "scope_canonical_id",
            "membership_semantic_relationship_id",
            "ordering_mode",
            "ordinal_property_id",
            "ordinal_value_type",
            "ordering_direction",
            "unique_ordinals",
            "contiguous",
            "member_order_encoding",
            "expected_cardinality",
            "minimum_cardinality",
            "maximum_cardinality",
            "required_role_ids",
            "member_set_hash",
            "ordered_member_tuple_hash",
            "authoritative_collection_hash",
            "domain_contract_hash",
            "hierarchy_hash",
            "identity_policy_hash",
            "completeness_requirement_id",
            "completeness_requirement_hash",
            "source_corpus_manifest_id",
            "source_corpus_manifest_hash",
            "source_unit_manifest_id",
            "source_unit_manifest_hash",
            "extraction_candidate_batch_id",
            "extraction_candidate_batch_hash",
            "manifest_hash",
        )
        for manifest_id, manifest_row in manifest_by_id.items():
            members = sorted(
                members_by_manifest.pop(manifest_id, []),
                key=lambda row: int(row["manifest_member_index"]),
            )
            source_entry = source_entry_by_id[manifest_id]
            if (
                source_entry.contract_version != "1.1.0"
                or source_entry.schema_hash != schema_hash
                or source_entry.content_hash != manifest_row["manifest_hash"]
                or source_entry.row_count != len(members)
                or source_entry.canonical_id_set_hash
                != manifest_row["member_set_hash"]
            ):
                raise ValueError(
                    f"projected RequiredMemberManifest {manifest_id} differs "
                    "from the anchored L3 manifest entry"
                )
            if (
                int(manifest_row["member_count"]) != len(members)
                or manifest_row["domain_contract_hash"]
                != serving.sealed_domain_contract_hash
                or [int(member["manifest_member_index"]) for member in members]
                != list(range(len(members)))
                or len({
                    str(member["member_canonical_id"]) for member in members
                })
                != len(members)
                or any(
                    any(member[field] != manifest_row[field] for field in common_fields)
                    for member in members
                )
            ):
                raise ValueError(
                    f"required-member rows differ from carried authority {manifest_id}"
                )
            try:
                ordering_payload = {
                    "mode": manifest_row["ordering_mode"],
                    "ordinal_property_id": manifest_row[
                        "ordinal_property_id"
                    ],
                    "ordinal_value_type": manifest_row[
                        "ordinal_value_type"
                    ],
                    "direction": manifest_row["ordering_direction"],
                    "unique_ordinals": manifest_row["unique_ordinals"],
                    "contiguous": manifest_row["contiguous"],
                    "member_order_encoding": manifest_row[
                        "member_order_encoding"
                    ],
                }
                ordering_policy = (
                    RequiredMemberOrderingPolicyV1_1.model_validate(
                        ordering_payload
                    )
                )
                if (
                    ordering_policy.model_dump(mode="json")
                    != ordering_payload
                ):
                    raise ValueError(
                        "required-member ordering policy is not canonical"
                    )
                member_payloads = tuple({
                    "member_canonical_id": member["member_canonical_id"],
                    "member_semantic_type_id": member[
                        "member_semantic_type_id"
                    ],
                    "member_role_id": member["member_role_id"],
                    "member_order": member["member_order"],
                    "candidate_id": member["candidate_id"],
                    "supporting_evidence_span_ids": member[
                        "supporting_evidence_span_ids"
                    ],
                    "member_hash": member["member_hash"],
                } for member in members)
                member_references = tuple(
                    RequiredMemberReferenceV1_1.model_validate(payload)
                    for payload in member_payloads
                )
                if any(
                    reference.model_dump(mode="json") != payload
                    for reference, payload in zip(
                        member_references,
                        member_payloads,
                        strict=True,
                    )
                ):
                    raise ValueError(
                        "required-member fields are not in canonical C0 form"
                    )
                manifest_identity = self.input_manifest.identity.model_dump(
                    mode="python"
                )
                manifest_identity.update({
                    "contract_kind": "c0.required_member_manifest",
                    "contract_version": "1.1.0",
                })
                validated_manifest = RequiredMemberManifestV1_1.model_validate({
                    "identity": manifest_identity,
                    "required_member_manifest_id": manifest_id,
                    "required_member_set_proposal_id": manifest_row[
                        "required_member_set_proposal_id"
                    ],
                    "required_member_set_proposal_hash": manifest_row[
                        "required_member_set_proposal_hash"
                    ],
                    "extraction_candidate_batch_id": manifest_row[
                        "extraction_candidate_batch_id"
                    ],
                    "extraction_candidate_batch_hash": manifest_row[
                        "extraction_candidate_batch_hash"
                    ],
                    "source_corpus_manifest_id": manifest_row[
                        "source_corpus_manifest_id"
                    ],
                    "source_corpus_manifest_hash": manifest_row[
                        "source_corpus_manifest_hash"
                    ],
                    "source_unit_manifest_id": manifest_row[
                        "source_unit_manifest_id"
                    ],
                    "source_unit_manifest_hash": manifest_row[
                        "source_unit_manifest_hash"
                    ],
                    "domain_contract_hash": manifest_row[
                        "domain_contract_hash"
                    ],
                    "completeness_requirement_id": manifest_row[
                        "completeness_requirement_id"
                    ],
                    "completeness_requirement_hash": manifest_row[
                        "completeness_requirement_hash"
                    ],
                    "hierarchy_hash": manifest_row["hierarchy_hash"],
                    "identity_policy_hash": manifest_row[
                        "identity_policy_hash"
                    ],
                    "scope_canonical_id": manifest_row[
                        "scope_canonical_id"
                    ],
                    "membership_semantic_relationship_id": manifest_row[
                        "membership_semantic_relationship_id"
                    ],
                    "ordering_policy": ordering_policy,
                    "expected_cardinality": manifest_row[
                        "expected_cardinality"
                    ],
                    "minimum_cardinality": manifest_row[
                        "minimum_cardinality"
                    ],
                    "maximum_cardinality": manifest_row[
                        "maximum_cardinality"
                    ],
                    "required_role_ids": manifest_row["required_role_ids"],
                    "members": member_references,
                    "member_set_hash": manifest_row["member_set_hash"],
                    "ordered_member_tuple_hash": manifest_row[
                        "ordered_member_tuple_hash"
                    ],
                    "authoritative_collection_hash": manifest_row[
                        "authoritative_collection_hash"
                    ],
                    "validator_name": manifest_row["validator_name"],
                    "validator_version": manifest_row["validator_version"],
                    "sealed_at_utc": self.receipt.started_at_utc,
                    "manifest_hash": manifest_row["manifest_hash"],
                })
                exact_manifest_fields = (
                    "required_member_manifest_id",
                    "required_member_set_proposal_id",
                    "required_member_set_proposal_hash",
                    "extraction_candidate_batch_id",
                    "extraction_candidate_batch_hash",
                    "source_corpus_manifest_id",
                    "source_corpus_manifest_hash",
                    "source_unit_manifest_id",
                    "source_unit_manifest_hash",
                    "domain_contract_hash",
                    "completeness_requirement_id",
                    "completeness_requirement_hash",
                    "hierarchy_hash",
                    "identity_policy_hash",
                    "scope_canonical_id",
                    "membership_semantic_relationship_id",
                    "expected_cardinality",
                    "minimum_cardinality",
                    "maximum_cardinality",
                    "member_set_hash",
                    "ordered_member_tuple_hash",
                    "authoritative_collection_hash",
                    "validator_name",
                    "validator_version",
                    "manifest_hash",
                )
                if (
                    any(
                        getattr(validated_manifest, field)
                        != manifest_row[field]
                        for field in exact_manifest_fields
                    )
                    or validated_manifest.required_role_ids
                    != tuple(manifest_row["required_role_ids"])
                    or validated_manifest.ordering_policy != ordering_policy
                    or validated_manifest.members != member_references
                ):
                    raise ValueError(
                        "required-member manifest physical order or fields "
                        "are not canonical"
                    )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "required-member rows do not satisfy carried C0 authority "
                    f"{manifest_id}"
                ) from exc
            ids = [
                f"{manifest_id}|{row['member_canonical_id']}" for row in members
            ]
            evidence = ProjectionEvidence(
                count=len(members),
                canonical_id_set_hash=canonical_sha256(sorted(ids)),
                row_fingerprint=canonical_sha256({
                    "manifest": manifest_row,
                    "members": members,
                }),
            )
            proof = proof_by_manifest[manifest_id]
            authority = proof.authority
            crosswalk_id = deterministic_contract_id(
                "l4-local-parquet-crosswalk",
                {
                    "required_member_manifest_id": manifest_id,
                    "schema_hash": canonical_sha256({
                        name: _schema_hash(name)
                        for name in (
                            "semantic_required_member_manifests",
                            "semantic_required_members",
                        )
                    }),
                },
            )
            crosswalk_hash = canonical_sha256({
                "publication_crosswalk_id": crosswalk_id,
                "source_tables": [
                    "semantic_required_member_manifests",
                    "semantic_required_members",
                ],
                "field_mapping": {
                    name: [
                        field.name for field in L4_PROJECTION_TABLE_SCHEMAS[name]
                    ]
                    for name in (
                        "semantic_required_member_manifests",
                        "semantic_required_members",
                    )
                },
                "authority": authority,
            })
            expected_proof_id = deterministic_contract_id(
                "projection-equivalence",
                {
                    "projection_kind": "parquet",
                    "required_member_manifest_id": manifest_id,
                    "source_projection_hash": serving.projection_hash,
                },
            )
            if (
                proof.projection_equivalence_id != expected_proof_id
                or _identity_lineage(proof.identity)
                != _identity_lineage(self.receipt.identity)
                or authority.required_member_manifest_schema_hash != schema_hash
                or authority.required_member_manifest_hash
                != manifest_row["manifest_hash"]
                or authority.authoritative_collection_hash
                != manifest_row["authoritative_collection_hash"]
                or authority.source_artifact_manifest_id
                != self.receipt.input_manifest_id
                or authority.source_artifact_manifest_hash
                != self.receipt.input_manifest_hash
                or proof.publication_crosswalk_id != crosswalk_id
                or proof.publication_crosswalk_hash != crosswalk_hash
                or proof.source_projection_id != serving.projection_id
                or proof.source_projection_hash != serving.projection_hash
                or proof.projection_kind != "parquet"
                or proof.expected != evidence
                or proof.compiled != evidence
                or proof.deployed != evidence
                or proof.read_back != evidence
                or proof.missing_canonical_ids
                or proof.extra_canonical_ids
                or not proof.equivalent
            ):
                raise ValueError(
                    f"projection equivalence differs for {manifest_id}"
                )
        if members_by_manifest:
            raise ValueError("required-member rows reference an unknown manifest")

    def _validate_table_artifact(
        self,
        table_name: str,
    ) -> tuple[Path, tuple[dict[str, object], ...]]:
        artifact_id = f"l4-table:{table_name}"
        entry = _one_artifact(self.manifest, artifact_id)
        path = self.root / f"{table_name}.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"sealed L4 table is missing: {path}")
        try:
            payload = path.read_bytes()
            table = pq.read_table(path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"sealed L4 table is unreadable: {path}") from exc
        rows = tuple(table.to_pylist())
        if (
            entry.contract_kind != f"l4.{table_name}"
            or entry.contract_version != "1.0.0"
            or entry.schema_hash != _schema_hash(table_name)
            or entry.content_hash != hashlib.sha256(payload).hexdigest()
            or entry.byte_count != len(payload)
            or entry.row_count != table.num_rows
            or entry.canonical_id_set_hash
            != _table_canonical_id_set_hash(
                table_name,
                rows,
            )
            or table.schema != L4_PROJECTION_TABLE_SCHEMAS[table_name]
        ):
            raise ValueError(
                f"sealed L4 table differs from its artifact manifest: {path}"
            )
        return path, rows

    def resolve(self, source_table_name: str) -> Path:
        table_name = source_table_name.removesuffix(".parquet")
        if table_name in {"entities", "relationships"}:
            raise ValueError("raw canonical tables are forbidden as schema-2 serving sources")
        if table_name not in _SEALED_L4_TABLES:
            raise ValueError(f"unknown sealed L4 serving table: {source_table_name!r}")
        return self._validate_table_artifact(table_name)[0]


def require_l5_publication_receipt(_source: SealedL4ServingSource) -> None:
    """Keep schema-2 product readiness fail-closed until L5 persists publication."""

    raise ValueError(
        "schema-2 serving is not product-ready until an L5 persisted publication "
        "receipt validates"
    )
