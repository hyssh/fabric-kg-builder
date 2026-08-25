"""Canonical/semantic Parquet source resolution shared by compile and deploy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.contracts.projection import (
    AuditProjection,
    SemanticServingProjection,
)
from fabric_kg_builder.contracts.publication import ProjectionEquivalence
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


def _table_canonical_id_set_hash(table_name: str, path: Path) -> str:
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
    rows = pq.read_table(path, columns=list(id_columns)).to_pylist()
    return canonical_sha256(
        sorted({
            "|".join(str(row[column]) for column in id_columns)
            for row in rows
        })
    )


@dataclass(frozen=True)
class SealedL4ServingSource:
    """Exact schema-2 source gate; raw canonical aliases are never accepted."""

    root: Path
    projection: SemanticServingProjection
    receipt: StageReceipt
    manifest: ArtifactManifest

    def __post_init__(self) -> None:
        if (
            self.receipt.stage_id != "L4"
            or self.receipt.stage_name != "schema2-audit-serving-projection"
            or self.receipt.stage_contract_version != "1.0.0"
            or self.receipt.status != "succeeded"
        ):
            raise ValueError("schema-2 serving requires a successful L4 receipt")
        if (
            self.receipt.output_manifest_id != self.manifest.artifact_manifest_id
            or self.receipt.output_manifest_hash != self.manifest.manifest_hash
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
        for table_name in L4_PROJECTION_TABLE_SCHEMAS:
            expected_files.add(f"{table_name}.parquet")
            self._validate_table_artifact(table_name)
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
                expected_payload = (
                    canonical_json(value) + "\n"
                ).encode("utf-8")
                row_count = 1
            elif artifact_id == "l4-semantic-serving-projection":
                value = SemanticServingProjection.model_validate_json(payload)
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
        actual_files = {
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file()
        }
        if actual_files != expected_files:
            raise ValueError("sealed L4 run has missing or extra files")

    def _validate_table_artifact(self, table_name: str) -> Path:
        artifact_id = f"l4-table:{table_name}"
        entry = _one_artifact(self.manifest, artifact_id)
        path = self.root / f"{table_name}.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"sealed L4 table is missing: {path}")
        try:
            payload = path.read_bytes()
            parquet = pq.ParquetFile(path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"sealed L4 table is unreadable: {path}") from exc
        if (
            entry.contract_kind != f"l4.{table_name}"
            or entry.contract_version != "1.0.0"
            or entry.schema_hash != _schema_hash(table_name)
            or entry.content_hash != hashlib.sha256(payload).hexdigest()
            or entry.byte_count != len(payload)
            or entry.row_count != parquet.metadata.num_rows
            or entry.canonical_id_set_hash
            != _table_canonical_id_set_hash(table_name, path)
            or parquet.schema_arrow != L4_PROJECTION_TABLE_SCHEMAS[table_name]
        ):
            raise ValueError(
                f"sealed L4 table differs from its artifact manifest: {path}"
            )
        return path

    def resolve(self, source_table_name: str) -> Path:
        table_name = source_table_name.removesuffix(".parquet")
        if table_name in {"entities", "relationships"}:
            raise ValueError("raw canonical tables are forbidden as schema-2 serving sources")
        if table_name not in _SEALED_L4_TABLES:
            raise ValueError(f"unknown sealed L4 serving table: {source_table_name!r}")
        return self._validate_table_artifact(table_name)


def require_l5_publication_receipt(_source: SealedL4ServingSource) -> None:
    """Keep schema-2 product readiness fail-closed until L5 persists publication."""

    raise ValueError(
        "schema-2 serving is not product-ready until an L5 persisted publication "
        "receipt validates"
    )
