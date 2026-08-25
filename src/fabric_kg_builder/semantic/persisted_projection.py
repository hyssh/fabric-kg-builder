"""Persisted Ontology and Graph projection readiness for SPEC-008A H3."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from fabric_kg_builder.semantic.artifact_validation import (
    ArtifactFinding,
    validate_graph_projection_parts,
    validate_ontology_projection_parts,
)
from fabric_kg_builder.semantic.schemas import (
    MaterializationPlan,
    MaterializationReceipt,
    MaterializedTableReceipt,
    PersistedProjectionReceipt,
    QueryReadiness,
    SemanticModelManifest,
    SourceTableAuthority,
)
from fabric_kg_builder.semantic.canonical_hash import (
    arrow_schema_hash,
    arrow_table_hash,
    canonical_hash,
)
from fabric_kg_builder.semantic.source_tables import (
    resolve_semantic_source_parquet,
    resolve_schema2_source_parquet,
    source_category,
    source_primary_key,
    source_table_candidates,
)
from fabric_kg_builder.serving.competency import (
    GQLQueryBuilder,
    _gql_extract_count,
    _gql_status_code,
    _gql_status_ok,
)


class PersistedProjectionError(RuntimeError):
    """Raised when submitted or persisted semantic projections are incomplete."""

    def __init__(self, findings: list[ArtifactFinding]) -> None:
        self.findings = tuple(findings)
        super().__init__(
            "; ".join(
                f"{finding.code}: {finding.message}"
                for finding in findings
            )
        )


class BoundTableReader(Protocol):
    """Read persisted Lakehouse tables for schema and row validation."""

    def read_table(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        table: str,
        columns: list[str] | None = None,
    ) -> Any:
        ...

    def list_tables(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
    ) -> list[str]:
        ...


class GQLReadinessClient(Protocol):
    """Execute validated GQL against one persisted Graph Model."""

    def execute_query(
        self,
        workspace_id: str,
        graph_model_id: str,
        query: str,
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class MaterializedTableEvidence:
    """Local preparation or persisted read-back evidence for one table."""

    semantic_id: str
    table_name: str
    source_path: str
    row_count: int
    columns: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class PersistedSurfaceEvidence:
    """Hash and definition counts from one persisted semantic surface."""

    projection_hash: str
    definition_counts: dict[str, int]


@dataclass(frozen=True)
class Schema2ProjectionAuthority:
    """Validated layer-4 projection and approved support-table authority."""

    contract_hash: str
    receipt_hash: str
    source_tables: dict[str, SourceTableAuthority]


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def write_safe_receipt(
    path: Path | str,
    payload: MaterializationReceipt | dict[str, Any],
) -> Path:
    """Atomically serialize a redacted receipt with secret/content canaries."""
    from fabric_kg_builder.release.redact import (
        assert_no_secrets,
        assert_no_source_content,
        redact_dict,
    )

    target = Path(path)
    raw = (
        payload.model_dump(mode="json")
        if isinstance(payload, MaterializationReceipt)
        else dict(payload)
    )
    safe = redact_dict(raw)
    assert_no_secrets(safe)
    assert_no_source_content(safe)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                safe,
                handle,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            handle.write("\n")
        os.replace(temp_name, target)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return target


def load_materialization_receipt(
    path: Path | str,
    *,
    plan: MaterializationPlan,
    semantic_model_manifest_hash: str,
    semantic_crosswalk_hash: str,
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str,
    require_live: bool = True,
) -> MaterializationReceipt:
    """Load a successful receipt and bind it to the current sealed authority."""
    target = Path(path)
    try:
        receipt = MaterializationReceipt.model_validate_json(
            target.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise PersistedProjectionError([
            ArtifactFinding(
                "MATERIALIZATION_RECEIPT_INVALID",
                f"Could not load materialization receipt: {exc}.",
            )
        ]) from exc
    findings: list[ArtifactFinding] = []
    if receipt.status != "succeeded":
        findings.append(ArtifactFinding(
            "MATERIALIZATION_RECEIPT_FAILED",
            "Materialization receipt does not prove a complete deployment.",
        ))
    if require_live and receipt.mock:
        findings.append(ArtifactFinding(
            "MATERIALIZATION_RECEIPT_MOCK",
            "Mock materialization cannot authorize live Ontology or Graph.",
        ))
    expected = {
        "workspace_id": workspace_id,
        "lakehouse_item_id": lakehouse_item_id,
        "schema_name": schema,
        "semantic_contract_hash": plan.semantic_contract_hash,
        "projection_receipt_hash": plan.projection_receipt_hash,
        "semantic_model_manifest_hash": semantic_model_manifest_hash,
        "semantic_crosswalk_hash": semantic_crosswalk_hash,
        "materialization_plan_hash": canonical_hash(
            plan.model_dump(mode="json")
        ),
    }
    for field, expected_value in expected.items():
        if getattr(receipt, field) != expected_value:
            findings.append(ArtifactFinding(
                "MATERIALIZATION_RECEIPT_AUTHORITY_DRIFT",
                f"Materialization receipt field '{field}' does not match "
                "the active sealed authority.",
            ))
    if receipt.source_tables != plan.source_tables:
        findings.append(ArtifactFinding(
            "MATERIALIZATION_RECEIPT_SOURCE_DRIFT",
            "Materialization receipt source authority differs from the plan.",
        ))
    specs = {
        table.table_name: table
        for table in [*plan.entity_tables, *plan.relationship_tables]
    }
    receipt_tables = {table.table_name: table for table in receipt.tables}
    if set(receipt_tables) != set(specs):
        findings.append(ArtifactFinding(
            "MATERIALIZATION_RECEIPT_TABLE_SET_DRIFT",
            "Materialization receipt typed-table set differs from the plan.",
        ))
    else:
        for table_name, spec in specs.items():
            table = receipt_tables[table_name]
            if (
                table.semantic_id != spec.semantic_id
                or table.source_table_name != spec.source_table_name
                or table.source_category != spec.source_category
                or table.planned_row_count != spec.planned_row_count
                or table.planned_row_hash != spec.planned_row_hash
                or table.planned_schema_hash != spec.planned_schema_hash
            ):
                findings.append(ArtifactFinding(
                    "MATERIALIZATION_RECEIPT_TABLE_DRIFT",
                    f"Materialization receipt table '{table_name}' differs "
                    "from the sealed plan.",
                ))
    if findings:
        raise PersistedProjectionError(findings)
    return receipt


def load_schema2_projection_authority(
    *,
    parquet_dir: Path | str,
    receipt_path: Path | str,
    expected_contract_hash: str,
    support_tables: set[str] | frozenset[str] = frozenset(),
) -> Schema2ProjectionAuthority:
    """Validate the layer-4 receipt and bind exact approved support sources."""
    receipt_file = Path(receipt_path)
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PersistedProjectionError([
            ArtifactFinding(
                "PROJECTION_RECEIPT_MISSING_OR_INVALID",
                f"Could not read schema-2 projection receipt: {exc}.",
            )
        ]) from exc
    if not isinstance(receipt, dict):
        raise PersistedProjectionError([
            ArtifactFinding(
                "PROJECTION_RECEIPT_INVALID",
                "Schema-2 projection receipt must contain an object.",
            )
        ])
    findings: list[ArtifactFinding] = []
    expected_fields = {
        "receipt_schema_version": "1.0",
        "schema_mode": "schema2",
        "status": "succeeded",
        "active_contract_hash": expected_contract_hash,
    }
    for field, expected in expected_fields.items():
        if receipt.get(field) != expected:
            findings.append(ArtifactFinding(
                "PROJECTION_RECEIPT_AUTHORITY_MISMATCH",
                f"Projection receipt field '{field}' must be {expected!r}, "
                f"found {receipt.get(field)!r}.",
            ))
    invariant_rows = receipt.get("invariants")
    invariant_by_gate = {
        str(item.get("gate")): item
        for item in invariant_rows
        if isinstance(item, dict) and item.get("gate")
    } if isinstance(invariant_rows, list) else {}
    for gate in ("SEM-100", "SEM-101", "SEM-102", "SEM-103", "SEM-104"):
        if invariant_by_gate.get(gate, {}).get("passed") is not True:
            findings.append(ArtifactFinding(
                "PROJECTION_RECEIPT_GATE_FAILED",
                f"Projection receipt does not prove {gate}.",
            ))

    requested_sources = {
        "semantic_entities",
        "semantic_relationships",
        "evidence",
        *support_tables,
    }
    source_authority: dict[str, SourceTableAuthority] = {}
    for table_name in sorted(requested_sources):
        try:
            category = source_category(table_name)
            path = resolve_schema2_source_parquet(parquet_dir, table_name)
            primary_key = source_primary_key(table_name)
        except (FileNotFoundError, ValueError) as exc:
            findings.append(ArtifactFinding(
                "PROJECTION_SOURCE_INVALID",
                str(exc),
            ))
            continue
        try:
            import pyarrow.parquet as pq  # type: ignore[import]

            table = pq.read_table(str(path))
        except Exception as exc:
            findings.append(ArtifactFinding(
                "PROJECTION_SOURCE_READ_FAILED",
                f"Could not read schema-2 source '{table_name}': {exc}.",
            ))
            continue
        if primary_key not in table.schema.names:
            findings.append(ArtifactFinding(
                "PROJECTION_SOURCE_KEY_MISSING",
                f"Schema-2 source '{table_name}' omits primary key "
                f"'{primary_key}'.",
            ))
            continue
        source_authority[table_name] = SourceTableAuthority(
            table_name=table_name,
            category=category,
            primary_key=primary_key,
            row_count=int(table.num_rows),
            table_hash=arrow_table_hash(table, primary_key),
            schema_hash=arrow_schema_hash(table.schema),
        )

    aggregate_hashes = receipt.get("aggregate_table_hashes")
    serving_counts = receipt.get("serving_counts")
    if not isinstance(aggregate_hashes, dict):
        findings.append(ArtifactFinding(
            "PROJECTION_RECEIPT_HASHES_MISSING",
            "Projection receipt omits aggregate_table_hashes.",
        ))
        aggregate_hashes = {}
    if not isinstance(serving_counts, dict):
        findings.append(ArtifactFinding(
            "PROJECTION_RECEIPT_COUNTS_MISSING",
            "Projection receipt omits serving_counts.",
        ))
        serving_counts = {}
    for table_name in ("semantic_entities", "semantic_relationships"):
        authority = source_authority.get(table_name)
        if authority is None:
            continue
        if aggregate_hashes.get(table_name) != authority.table_hash:
            findings.append(ArtifactFinding(
                "PROJECTION_SOURCE_HASH_MISMATCH",
                f"Source '{table_name}' hash does not match the successful "
                "projection receipt.",
            ))
        if serving_counts.get(table_name) != authority.row_count:
            findings.append(ArtifactFinding(
                "PROJECTION_SOURCE_COUNT_MISMATCH",
                f"Source '{table_name}' count does not match the successful "
                "projection receipt.",
            ))
    evidence_authority = source_authority.get("evidence")
    if evidence_authority is not None and (
        aggregate_hashes.get("input_evidence") != evidence_authority.table_hash
    ):
        findings.append(ArtifactFinding(
            "PROJECTION_EVIDENCE_HASH_MISMATCH",
            "evidence.parquet hash does not match input_evidence in the "
            "successful projection receipt.",
        ))
    if findings:
        raise PersistedProjectionError(findings)
    return Schema2ProjectionAuthority(
        contract_hash=expected_contract_hash,
        receipt_hash=canonical_hash(receipt),
        source_tables=source_authority,
    )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def resolve_source_parquet(
    parquet_dir: Path | str,
    source_table_name: str,
) -> Path:
    """Resolve an exact source table with canonical/semantic compatibility."""
    try:
        return resolve_semantic_source_parquet(
            parquet_dir,
            source_table_name,
        )
    except ValueError as exc:
        raise PersistedProjectionError([
            ArtifactFinding(
                "MATERIALIZATION_SOURCE_NAME_INVALID",
                str(exc),
            )
        ]) from exc
    except FileNotFoundError as exc:
        candidates = source_table_candidates(source_table_name)
        raise PersistedProjectionError([
            ArtifactFinding(
                "MATERIALIZATION_SOURCE_MISSING",
                f"{exc} Compatible candidates: {list(candidates)}.",
            )
        ]) from exc


def decode_fabric_definition_parts(
    definition: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Decode a Fabric getDefinition response into path-keyed JSON parts."""
    payload = definition.get("definition", definition)
    if not isinstance(payload, dict):
        raise PersistedProjectionError([
            ArtifactFinding(
                "PERSISTED_DEFINITION_INVALID",
                "Fabric definition read-back must be a JSON object.",
            )
        ])
    raw_parts = payload.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise PersistedProjectionError([
            ArtifactFinding(
                "PERSISTED_DEFINITION_EMPTY",
                "Fabric definition read-back contains no definition parts.",
            )
        ])

    decoded: dict[str, dict[str, Any]] = {}
    findings: list[ArtifactFinding] = []
    for ordinal, part in enumerate(raw_parts):
        if not isinstance(part, dict):
            findings.append(ArtifactFinding(
                "PERSISTED_PART_INVALID",
                f"Definition part {ordinal} is not an object.",
            ))
            continue
        path = str(part.get("path") or "")
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            findings.append(ArtifactFinding(
                "PERSISTED_PART_PATH_INVALID",
                f"Definition part {ordinal} has unsafe path {path!r}.",
            ))
            continue
        if path in decoded:
            findings.append(ArtifactFinding(
                "PERSISTED_PART_DUPLICATE",
                f"Definition contains duplicate part path '{path}'.",
            ))
            continue

        payload_json = part.get("payload_json")
        if isinstance(payload_json, dict):
            decoded[path] = payload_json
            continue
        if part.get("payloadType") != "InlineBase64":
            findings.append(ArtifactFinding(
                "PERSISTED_PART_ENCODING_INVALID",
                f"Definition part '{path}' must use InlineBase64.",
            ))
            continue
        try:
            raw = base64.b64decode(
                str(part.get("payload") or ""),
                validate=True,
            ).decode("utf-8")
            value = json.loads(raw)
        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            findings.append(ArtifactFinding(
                "PERSISTED_PART_PAYLOAD_INVALID",
                f"Definition part '{path}' is not valid Base64 JSON: {exc}.",
            ))
            continue
        if not isinstance(value, dict):
            findings.append(ArtifactFinding(
                "PERSISTED_PART_PAYLOAD_INVALID",
                f"Definition part '{path}' must decode to an object.",
            ))
            continue
        decoded[path] = value

    if findings:
        raise PersistedProjectionError(findings)
    return decoded


def persisted_parts_hash(parts: dict[str, dict[str, Any]]) -> str:
    """Hash decoded persisted parts independently of API ordering."""
    return _canonical_hash(
        [
            {"path": path, "payload_json": parts[path]}
            for path in sorted(parts)
        ]
    )


def _filter_table(
    table: Any,
    column_name: str | None,
    value: str | None,
) -> Any:
    if not column_name:
        return table
    import pyarrow as pa  # type: ignore[import]
    import pyarrow.compute as pc  # type: ignore[import]

    if column_name not in table.schema.names:
        raise PersistedProjectionError([
            ArtifactFinding(
                "MATERIALIZATION_FILTER_COLUMN_MISSING",
                f"Source table omits filter column '{column_name}'.",
            )
        ])
    field = table.schema.field(column_name)
    try:
        scalar = pa.scalar(value, type=field.type)
        mask = pc.fill_null(pc.equal(table[column_name], scalar), False)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        raise PersistedProjectionError([
            ArtifactFinding(
                "MATERIALIZATION_FILTER_VALUE_INVALID",
                f"Filter {column_name}={value!r} is incompatible with "
                f"{field.type}: {exc}.",
            )
        ]) from exc
    return table.filter(mask)


def _type_compatible(data_type: str, arrow_type: Any) -> bool:
    import pyarrow.types as pat  # type: ignore[import]

    if data_type in {"string", "uri", "json"}:
        return pat.is_string(arrow_type) or pat.is_large_string(arrow_type)
    if data_type == "integer":
        return pat.is_integer(arrow_type)
    if data_type == "number":
        return (
            pat.is_integer(arrow_type)
            or pat.is_floating(arrow_type)
            or pat.is_decimal(arrow_type)
        )
    if data_type == "boolean":
        return pat.is_boolean(arrow_type)
    if data_type == "datetime":
        return (
            pat.is_timestamp(arrow_type)
            or pat.is_string(arrow_type)
            or pat.is_large_string(arrow_type)
        )
    if data_type == "date":
        return (
            pat.is_date(arrow_type)
            or pat.is_string(arrow_type)
            or pat.is_large_string(arrow_type)
        )
    return False


def _coerce_complex_scalar_columns(table: Any, columns: list[Any]) -> Any:
    """Serialize complex Arrow values for scalar Ontology properties."""
    import pyarrow as pa  # type: ignore[import]
    import pyarrow.types as pat  # type: ignore[import]

    scalar_text_types = {"string", "uri", "json"}
    for column in columns:
        if column.data_type not in scalar_text_types:
            continue
        field_index = table.schema.get_field_index(column.column_name)
        if field_index < 0:
            continue
        arrow_type = table.schema.field(field_index).type
        if not (
            pat.is_list(arrow_type)
            or pat.is_large_list(arrow_type)
            or pat.is_fixed_size_list(arrow_type)
            or pat.is_map(arrow_type)
            or pat.is_struct(arrow_type)
        ):
            continue
        values = [
            None
            if value is None
            else json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for value in table[column.column_name].to_pylist()
        ]
        table = table.set_column(
            field_index,
            column.column_name,
            pa.array(values, type=pa.string()),
        )
    return table


def _validate_arrow_table(
    *,
    semantic_id: str,
    table_name: str,
    table: Any,
    columns: list[Any],
    key_column: str,
    required_rows: int,
    require_exact_columns: bool,
) -> list[ArtifactFinding]:
    findings: list[ArtifactFinding] = []
    expected_names = [column.column_name for column in columns]
    if len(expected_names) != len(set(expected_names)):
        findings.append(ArtifactFinding(
            "MATERIALIZATION_COLUMNS_DUPLICATE",
            f"Materialization table '{table_name}' has duplicate columns.",
        ))
        return findings
    actual_names = list(table.schema.names)
    missing = sorted(set(expected_names) - set(actual_names))
    extra = sorted(set(actual_names) - set(expected_names))
    if missing:
        findings.append(ArtifactFinding(
            "BOUND_TABLE_COLUMNS_MISSING",
            f"Table '{table_name}' omits required columns {missing}.",
        ))
    if require_exact_columns and extra:
        findings.append(ArtifactFinding(
            "BOUND_TABLE_COLUMNS_STALE",
            f"Table '{table_name}' contains unapproved columns {extra}.",
        ))
    for column in columns:
        if column.column_name not in actual_names:
            continue
        arrow_column = table[column.column_name]
        if not column.nullable and arrow_column.null_count:
            findings.append(ArtifactFinding(
                "BOUND_TABLE_REQUIRED_VALUE_NULL",
                f"Table '{table_name}' has {arrow_column.null_count} null "
                f"value(s) in required column '{column.column_name}'.",
            ))
        if not _type_compatible(
            column.data_type,
            table.schema.field(column.column_name).type,
        ):
            findings.append(ArtifactFinding(
                "BOUND_TABLE_TYPE_MISMATCH",
                f"Table '{table_name}' column '{column.column_name}' has "
                f"type {table.schema.field(column.column_name).type}, expected "
                f"semantic type '{column.data_type}'.",
            ))
    if key_column not in actual_names:
        findings.append(ArtifactFinding(
            "BOUND_TABLE_KEY_MISSING",
            f"Table '{table_name}' omits key column '{key_column}'.",
        ))
    else:
        key_values = table[key_column].to_pylist()
        if any(value is None or str(value) == "" for value in key_values):
            findings.append(ArtifactFinding(
                "BOUND_TABLE_KEY_EMPTY",
                f"Table '{table_name}' contains an empty key in '{key_column}'.",
            ))
        normalized = [str(value) for value in key_values if value is not None]
        if len(normalized) != len(set(normalized)):
            findings.append(ArtifactFinding(
                "BOUND_TABLE_KEY_DUPLICATE",
                f"Table '{table_name}' contains duplicate '{key_column}' values.",
            ))
    if table.num_rows < required_rows:
        findings.append(ArtifactFinding(
            "BOUND_TABLE_ROW_COUNT_INSUFFICIENT",
            f"Table '{table_name}' has {table.num_rows} rows; "
            f"{required_rows} are required for '{semantic_id}'.",
        ))
    return findings


def materialize_semantic_tables(
    *,
    parquet_dir: Path | str,
    plan: MaterializationPlan,
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str = "dbo",
    token_provider: Callable[[], str] | None = None,
    table_writer: Callable[[str, Any], None] | None = None,
    mock: bool = False,
) -> dict[str, MaterializedTableEvidence]:
    """Materialize every contract-owned table before Ontology mutation."""
    if not workspace_id or not lakehouse_item_id:
        raise ValueError(
            "workspace_id and lakehouse_item_id are required for materialization."
        )
    specs = [*plan.entity_tables, *plan.relationship_tables]
    table_names = [spec.table_name for spec in specs]
    if len(table_names) != len(set(table_names)):
        raise PersistedProjectionError([
            ArtifactFinding(
                "MATERIALIZATION_TABLE_DUPLICATE",
                "Materialization plan contains duplicate physical table names.",
            )
        ])
    if mock:
        return {
            spec.table_name: MaterializedTableEvidence(
                semantic_id=spec.semantic_id,
                table_name=spec.table_name,
                source_path="planned",
                row_count=0,
                columns=tuple(
                    column.column_name for column in spec.columns
                ),
                status="planned",
            )
            for spec in specs
        }

    prepared = prepare_semantic_tables(
        parquet_dir=parquet_dir,
        plan=plan,
        enforce_planned_hashes=True,
    )

    if table_writer is None:
        from deltalake import write_deltalake  # type: ignore[import]

        if token_provider is None:
            from azure.identity import DefaultAzureCredential  # type: ignore[import]

            from fabric_kg_builder.azure_identity import (
                default_azure_credential,
            )

            credential = default_azure_credential()
            token_provider = lambda: credential.get_token(
                "https://storage.azure.com/.default"
            ).token
        storage_options = {
            "bearer_token": token_provider(),
            "use_fabric_endpoint": "true",
        }

        def table_writer(table_name: str, table: Any) -> None:
            uri = (
                f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/"
                f"{lakehouse_item_id}/Tables/{schema}/{table_name}"
            )
            write_deltalake(
                uri,
                table,
                mode="overwrite",
                schema_mode="overwrite",
                storage_options=storage_options,
            )

    written: dict[str, MaterializedTableEvidence] = {}
    for table_name in sorted(prepared):
        table, evidence = prepared[table_name]
        table_writer(table_name, table)
        written[table_name] = MaterializedTableEvidence(
            semantic_id=evidence.semantic_id,
            table_name=evidence.table_name,
            source_path=evidence.source_path,
            row_count=evidence.row_count,
            columns=evidence.columns,
            status="ok",
        )
    return written


def prepare_semantic_tables(
    *,
    parquet_dir: Path | str,
    plan: MaterializationPlan,
    enforce_planned_hashes: bool,
    strict_schema2: bool | None = None,
) -> dict[str, tuple[Any, MaterializedTableEvidence]]:
    """Prepare all typed rows and enforce schema-2 publication boundaries."""
    import pyarrow as pa  # type: ignore[import]
    import pyarrow.compute as pc  # type: ignore[import]
    import pyarrow.parquet as pq  # type: ignore[import]

    specs = [*plan.entity_tables, *plan.relationship_tables]
    availability = {
        item.semantic_id: item for item in plan.data_availability
    }
    entity_table_by_id = {
        table.semantic_id: table for table in plan.entity_tables
    }
    relationship_table_by_id = {
        table.semantic_id: table for table in plan.relationship_tables
    }
    strict_schema2 = (
        plan.source_taxonomy_version is not None
        if strict_schema2 is None
        else strict_schema2
    )
    source_cache: dict[Path, Any] = {}
    prepared: dict[str, tuple[Any, MaterializedTableEvidence]] = {}
    findings: list[ArtifactFinding] = []

    published_by_type: dict[str, set[str]] = {}
    all_published_ids: set[str] = set()
    evidence_ids: set[str] = set()
    evidence_chunk_by_id: dict[str, str] = {}
    approved_chunk_ids: set[str] = set()
    if strict_schema2:
        semantic_entities_path = resolve_schema2_source_parquet(
            parquet_dir,
            "semantic_entities",
        )
        evidence_path = resolve_schema2_source_parquet(parquet_dir, "evidence")
        semantic_entities = pq.read_table(str(semantic_entities_path))
        evidence = pq.read_table(str(evidence_path))
        entity_ids = semantic_entities["entity_id"].to_pylist()
        semantic_type_ids = (
            semantic_entities["semantic_type_id"].to_pylist()
            if "semantic_type_id" in semantic_entities.schema.names
            else [None] * semantic_entities.num_rows
        )
        for entity_id, semantic_type_id in zip(
            entity_ids,
            semantic_type_ids,
        ):
            if entity_id is None:
                continue
            identity = str(entity_id)
            all_published_ids.add(identity)
            if semantic_type_id:
                published_by_type.setdefault(
                    str(semantic_type_id), set()
                ).add(identity)
        evidence_ids = {
            str(value)
            for value in evidence["evidence_id"].to_pylist()
            if value is not None
        }
        if "chunk_id" in evidence.schema.names:
            evidence_chunk_by_id = {
                str(evidence_id): str(chunk_id)
                for evidence_id, chunk_id in zip(
                    evidence["evidence_id"].to_pylist(),
                    evidence["chunk_id"].to_pylist(),
                )
                if evidence_id is not None and chunk_id is not None
            }
        if any(
            table.source_table_name == "chunks"
            for table in plan.entity_tables
        ):
            chunks_path = resolve_schema2_source_parquet(
                parquet_dir,
                "chunks",
            )
            chunks = pq.read_table(str(chunks_path), columns=["chunk_id"])
            approved_chunk_ids = {
                str(chunk_id)
                for chunk_id in chunks["chunk_id"].to_pylist()
                if chunk_id is not None
                and str(chunk_id) in all_published_ids
            }

    for spec in specs:
        source_table_name = str(spec.source_table_name or "")
        try:
            source_path = (
                resolve_schema2_source_parquet(
                    parquet_dir,
                    source_table_name,
                )
                if strict_schema2
                else resolve_source_parquet(parquet_dir, source_table_name)
            )
        except (FileNotFoundError, ValueError, PersistedProjectionError) as exc:
            if isinstance(exc, PersistedProjectionError):
                findings.extend(exc.findings)
            else:
                findings.append(ArtifactFinding(
                    "MATERIALIZATION_SOURCE_INVALID",
                    str(exc),
                ))
            continue
        if source_path not in source_cache:
            source_cache[source_path] = pq.read_table(str(source_path))
        try:
            sliced = _filter_table(
                source_cache[source_path],
                spec.source_filter_column,
                spec.source_filter_value,
            )
        except PersistedProjectionError as exc:
            findings.extend(exc.findings)
            continue

        if strict_schema2:
            if spec.source_category != source_category(source_table_name):
                findings.append(ArtifactFinding(
                    "MATERIALIZATION_SOURCE_CATEGORY_MISMATCH",
                    f"Table '{spec.table_name}' source category does not match "
                    f"the closed taxonomy for '{source_table_name}'.",
                ))
                continue
            if hasattr(spec, "entity_id_column"):
                identity_column = spec.entity_id_column
                if (
                    spec.source_category
                    == "semantic_entity_projection"
                    and "semantic_type_id" in sliced.schema.names
                ):
                    sliced = _filter_table(
                        sliced,
                        "semantic_type_id",
                        spec.semantic_id,
                    )
                elif spec.source_category == "canonical_support_entity":
                    if identity_column not in sliced.schema.names:
                        findings.append(ArtifactFinding(
                            "SUPPORT_IDENTITY_COLUMN_MISSING",
                            f"Support source '{source_table_name}' omits "
                            f"identity column '{identity_column}'.",
                        ))
                        continue
                    allowed_ids = published_by_type.get(spec.semantic_id, set())
                    mask = pc.is_in(
                        sliced[identity_column],
                        value_set=pa.array(
                            sorted(allowed_ids),
                            type=sliced.schema.field(identity_column).type,
                        ),
                    )
                    sliced = sliced.filter(pc.fill_null(mask, False))
            else:
                if (
                    spec.source_category
                    != "semantic_relationship_projection"
                ):
                    findings.append(ArtifactFinding(
                        "RELATIONSHIP_SOURCE_NOT_SEMANTIC",
                        f"Schema-2 relationship table '{spec.table_name}' must "
                        "source semantic_relationships.",
                    ))
                    continue
                if "semantic_relationship_id" in sliced.schema.names:
                    sliced = _filter_table(
                        sliced,
                        "semantic_relationship_id",
                        spec.semantic_id,
                    )
                row_values = sliced.to_pylist()
                is_evidenced_by = (
                    spec.semantic_id.endswith(
                        (":evidenced-by", ":evidenced_by")
                    )
                    or any(
                        str(row.get("relationship_type") or "")
                        .casefold()
                        .replace("-", "_")
                        == "evidenced_by"
                        for row in row_values
                    )
                )
                invalid_rows = [
                    str(row.get(spec.relationship_id_column) or "<missing>")
                    for row in row_values
                    if (
                        row.get("assertion_state") != "asserted"
                        or not row.get(spec.evidence_column or "evidence_id")
                        or str(
                            row.get(spec.evidence_column or "evidence_id")
                        ) not in evidence_ids
                        or str(row.get(spec.source_column) or "")
                        not in all_published_ids
                        or str(row.get(spec.target_column) or "")
                        not in all_published_ids
                        or (
                            is_evidenced_by
                            and (
                                evidence_chunk_by_id.get(str(
                                    row.get(
                                        spec.evidence_column or "evidence_id"
                                    )
                                    or ""
                                ))
                                != str(row.get(spec.target_column) or "")
                                or str(row.get(spec.target_column) or "")
                                not in approved_chunk_ids
                            )
                        )
                    )
                ]
                if invalid_rows:
                    findings.append(ArtifactFinding(
                        "RELATIONSHIP_PUBLICATION_INVALID",
                        f"Typed relationship table '{spec.table_name}' contains "
                        f"{len(invalid_rows)} non-serving row(s).",
                    ))
                    continue

        expected_names = [column.column_name for column in spec.columns]
        missing = sorted(set(expected_names) - set(sliced.schema.names))
        if missing:
            findings.append(ArtifactFinding(
                "MATERIALIZATION_SOURCE_COLUMNS_MISSING",
                f"Source '{source_path.name}' for '{spec.semantic_id}' "
                f"omits {missing}.",
            ))
            continue
        projected = _coerce_complex_scalar_columns(
            sliced.select(expected_names),
            spec.columns,
        )
        required_rows = availability[spec.semantic_id].required_rows
        key_column = (
            spec.entity_id_column
            if hasattr(spec, "entity_id_column")
            else spec.relationship_id_column
        )
        findings.extend(_validate_arrow_table(
            semantic_id=spec.semantic_id,
            table_name=spec.table_name,
            table=projected,
            columns=spec.columns,
            key_column=key_column,
            required_rows=required_rows,
            require_exact_columns=True,
        ))
        row_hash = arrow_table_hash(projected, key_column)
        if enforce_planned_hashes and (
            spec.planned_row_count != projected.num_rows
            or spec.planned_row_hash != row_hash
            or spec.planned_schema_hash != arrow_schema_hash(projected.schema)
        ):
            findings.append(ArtifactFinding(
                "MATERIALIZATION_PLAN_ROW_DRIFT",
                f"Prepared table '{spec.table_name}' differs from its sealed "
                "planned row hash/count.",
            ))
        prepared[spec.table_name] = (
            projected,
            MaterializedTableEvidence(
                semantic_id=spec.semantic_id,
                table_name=spec.table_name,
                source_path=source_table_name,
                row_count=projected.num_rows,
                columns=tuple(expected_names),
                status="prepared",
            ),
        )
    if findings:
        raise PersistedProjectionError(findings)
    return prepared


def deploy_schema2_materialization(
    *,
    environment: str,
    parquet_dir: Path | str,
    plan: MaterializationPlan,
    semantic_model_manifest_hash: str,
    semantic_crosswalk_hash: str,
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str,
    table_writer: Callable[[str, Any], None],
    table_reader: BoundTableReader | None,
    prepared_tables: dict[
        str, tuple[Any, MaterializedTableEvidence]
    ] | None = None,
    mock: bool = False,
) -> MaterializationReceipt:
    """Deploy and read back one sealed schema-2 typed-table set."""
    if (
        plan.source_taxonomy_version is None
        or not plan.projection_receipt_hash
        or not plan.semantic_contract_hash
    ):
        raise PersistedProjectionError([
            ArtifactFinding(
                "MATERIALIZATION_AUTHORITY_UNSEALED",
                "Schema-2 materialization requires a sealed source taxonomy "
                "and projection receipt.",
            )
        ])
    prepared = prepared_tables or prepare_semantic_tables(
        parquet_dir=parquet_dir,
        plan=plan,
        enforce_planned_hashes=True,
    )
    expected_table_names = {
        table.table_name
        for table in [*plan.entity_tables, *plan.relationship_tables]
    }
    if set(prepared) != expected_table_names:
        raise PersistedProjectionError([
            ArtifactFinding(
                "MATERIALIZATION_PREPARED_SET_DRIFT",
                "Prepared typed-table set differs from the sealed plan.",
            )
        ])
    if any(not name.startswith("kg_") for name in expected_table_names):
        raise PersistedProjectionError([
            ArtifactFinding(
                "MATERIALIZATION_MANAGED_NAMESPACE_INVALID",
                "Schema-2 contract-owned typed tables must use the managed "
                "'kg_' namespace.",
            )
        ])
    source_by_name = {
        source.table_name: source for source in plan.source_tables
    }
    spec_by_table = {
        spec.table_name: spec
        for spec in [*plan.entity_tables, *plan.relationship_tables]
    }

    def receipt_row(
        table_name: str,
        *,
        status: str,
        persisted_table: Any | None = None,
        failure_code: str | None = None,
        failure: Exception | str | None = None,
    ) -> MaterializedTableReceipt:
        spec = spec_by_table[table_name]
        source = source_by_name[str(spec.source_table_name)]
        persisted_row_hash = None
        persisted_row_count = None
        persisted_schema_hash = None
        if persisted_table is not None:
            key_column = (
                spec.entity_id_column
                if hasattr(spec, "entity_id_column")
                else spec.relationship_id_column
            )
            persisted_row_hash = arrow_table_hash(
                persisted_table,
                key_column,
            )
            persisted_row_count = int(persisted_table.num_rows)
            persisted_schema_hash = arrow_schema_hash(persisted_table.schema)
        failure_message = None
        failure_type = None
        if failure is not None:
            failure_type = (
                type(failure).__name__
                if isinstance(failure, Exception)
                else "MaterializationFailure"
            )
            failure_message = "[REDACTED]"
        return MaterializedTableReceipt(
            semantic_id=spec.semantic_id,
            table_name=table_name,
            deployed_identity=(
                f"{workspace_id}/{lakehouse_item_id}/Tables/{schema}/"
                f"{table_name}"
            ),
            source_table_name=source.table_name,
            source_category=source.category,
            source_table_hash=source.table_hash,
            source_row_count=source.row_count,
            source_schema_hash=source.schema_hash,
            planned_row_hash=str(spec.planned_row_hash),
            planned_row_count=int(spec.planned_row_count or 0),
            planned_schema_hash=str(spec.planned_schema_hash),
            persisted_row_hash=persisted_row_hash,
            persisted_row_count=persisted_row_count,
            persisted_schema_hash=persisted_schema_hash,
            status=status,
            failure_code=failure_code,
            failure_type=failure_type,
            failure_message=failure_message,
        )

    table_receipts: dict[str, MaterializedTableReceipt] = {}
    ordered_tables = sorted(prepared)
    expected_managed_tables = sorted(expected_table_names)
    actual_managed_tables: list[str] = []
    if mock:
        for table_name in ordered_tables:
            table_receipts[table_name] = receipt_row(
                table_name,
                status="planned",
            )
    else:
        write_failed = False
        written_tables: list[str] = []
        for table_name in ordered_tables:
            if write_failed:
                table_receipts[table_name] = receipt_row(
                    table_name,
                    status="not_attempted",
                    failure_code="PRIOR_TABLE_WRITE_FAILED",
                )
                continue
            table, _evidence = prepared[table_name]
            try:
                table_writer(table_name, table)
                written_tables.append(table_name)
            except Exception as exc:
                write_failed = True
                table_receipts[table_name] = receipt_row(
                    table_name,
                    status="failed",
                    failure_code="TABLE_WRITE_FAILED",
                    failure=exc,
                )
        if write_failed:
            for table_name in written_tables:
                table_receipts[table_name] = receipt_row(
                    table_name,
                    status="failed",
                    failure_code="UNVERIFIED_PARTIAL_WRITE",
                    failure=(
                        "Table write completed before a later table failed; "
                        "the partial deployment is not authoritative."
                    ),
                )
        if not write_failed:
            if table_reader is None:
                raise ValueError(
                    "Live schema-2 materialization requires table_reader."
                )
            for table_name in ordered_tables:
                spec = spec_by_table[table_name]
                try:
                    persisted = table_reader.read_table(
                        workspace_id,
                        lakehouse_item_id,
                        schema,
                        table_name,
                    )
                    key_column = (
                        spec.entity_id_column
                        if hasattr(spec, "entity_id_column")
                        else spec.relationship_id_column
                    )
                    persisted_hash = arrow_table_hash(
                        persisted,
                        key_column,
                    )
                    persisted_schema_hash = arrow_schema_hash(
                        persisted.schema
                    )
                    if (
                        persisted.num_rows != spec.planned_row_count
                        or persisted_hash != spec.planned_row_hash
                        or persisted_schema_hash != spec.planned_schema_hash
                    ):
                        raise ValueError(
                            "Persisted row hash/count/schema does not match "
                            "the sealed typed-table plan."
                        )
                    table_receipts[table_name] = receipt_row(
                        table_name,
                        status="ok",
                        persisted_table=persisted,
                    )
                except Exception as exc:
                    table_receipts[table_name] = receipt_row(
                        table_name,
                        status="failed",
                        failure_code="TABLE_READBACK_FAILED",
                        failure=exc,
                    )

    if not mock and table_reader is not None:
        try:
            actual_managed_tables = sorted({
                table_name
                for table_name in table_reader.list_tables(
                    workspace_id,
                    lakehouse_item_id,
                    schema,
                )
                if table_name.startswith("kg_")
            })
        except Exception:
            actual_managed_tables = []
    succeeded = (
        bool(table_receipts)
        and all(
            receipt.status == "ok" for receipt in table_receipts.values()
        )
        and actual_managed_tables == expected_managed_tables
    )
    return MaterializationReceipt(
        status="succeeded" if succeeded else "failed",
        environment=environment,
        workspace_id=workspace_id,
        lakehouse_item_id=lakehouse_item_id,
        schema_name=schema,
        semantic_contract_hash=plan.semantic_contract_hash,
        projection_receipt_hash=plan.projection_receipt_hash,
        semantic_model_manifest_hash=semantic_model_manifest_hash,
        semantic_crosswalk_hash=semantic_crosswalk_hash,
        materialization_plan_hash=canonical_hash(
            plan.model_dump(mode="json")
        ),
        source_tables=plan.source_tables,
        tables=[
            table_receipts[table_name]
            for table_name in sorted(table_receipts)
        ],
        expected_managed_tables=expected_managed_tables,
        actual_managed_tables=actual_managed_tables,
        emitted_at_utc=_utc_now(),
        mock=mock,
    )


def validate_bound_tables(
    *,
    plan: MaterializationPlan,
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str,
    table_reader: BoundTableReader,
    materialization_receipt: MaterializationReceipt | None = None,
    managed_table_prefix: str | None = None,
) -> dict[str, int]:
    """Read persisted bound tables and validate exact schema, keys, and counts."""
    availability = {
        item.semantic_id: item for item in plan.data_availability
    }
    findings: list[ArtifactFinding] = []
    counts: dict[str, int] = {}
    receipt_by_table = {
        table.table_name: table
        for table in (
            materialization_receipt.tables
            if materialization_receipt is not None
            else []
        )
    }
    if managed_table_prefix is not None:
        expected_managed = {
            spec.table_name
            for spec in [*plan.entity_tables, *plan.relationship_tables]
            if spec.table_name.startswith(managed_table_prefix)
        }
        try:
            actual_managed = {
                table_name
                for table_name in table_reader.list_tables(
                    workspace_id,
                    lakehouse_item_id,
                    schema,
                )
                if table_name.startswith(managed_table_prefix)
            }
        except Exception as exc:
            findings.append(ArtifactFinding(
                "BOUND_TABLE_NAMESPACE_READ_FAILED",
                f"Could not enumerate managed typed tables: {exc}.",
            ))
        else:
            if actual_managed != expected_managed:
                findings.append(ArtifactFinding(
                    "BOUND_TABLE_NAMESPACE_DRIFT",
                    "Managed typed-table namespace differs from the sealed "
                    f"plan. Missing={sorted(expected_managed - actual_managed)}; "
                    f"extra={sorted(actual_managed - expected_managed)}.",
                ))
            if materialization_receipt is not None and (
                sorted(expected_managed)
                != materialization_receipt.expected_managed_tables
                or sorted(actual_managed)
                != materialization_receipt.actual_managed_tables
            ):
                findings.append(ArtifactFinding(
                    "BOUND_TABLE_NAMESPACE_RECEIPT_DRIFT",
                    "Managed typed-table namespace differs from the "
                    "materialization receipt.",
                ))
    for spec in [*plan.entity_tables, *plan.relationship_tables]:
        try:
            table = table_reader.read_table(
                workspace_id,
                lakehouse_item_id,
                schema,
                spec.table_name,
            )
        except Exception as exc:
            findings.append(ArtifactFinding(
                "BOUND_TABLE_READ_FAILED",
                f"Could not read '{schema}.{spec.table_name}': {exc}.",
            ))
            continue
        key_column = (
            spec.entity_id_column
            if hasattr(spec, "entity_id_column")
            else spec.relationship_id_column
        )
        findings.extend(_validate_arrow_table(
            semantic_id=spec.semantic_id,
            table_name=spec.table_name,
            table=table,
            columns=spec.columns,
            key_column=key_column,
            required_rows=availability[spec.semantic_id].required_rows,
            require_exact_columns=True,
        ))
        key_column = (
            spec.entity_id_column
            if hasattr(spec, "entity_id_column")
            else spec.relationship_id_column
        )
        row_hash = arrow_table_hash(table, key_column)
        schema_hash = arrow_schema_hash(table.schema)
        if (
            spec.planned_row_count is not None
            and (
                table.num_rows != spec.planned_row_count
                or row_hash != spec.planned_row_hash
                or schema_hash != spec.planned_schema_hash
            )
        ):
            findings.append(ArtifactFinding(
                "BOUND_TABLE_EXACT_DRIFT",
                f"Persisted table '{spec.table_name}' differs from the sealed "
                "planned hash/count/schema.",
            ))
        receipt_table = receipt_by_table.get(spec.table_name)
        if materialization_receipt is not None and (
            receipt_table is None
            or receipt_table.persisted_row_count != table.num_rows
            or receipt_table.persisted_row_hash != row_hash
            or receipt_table.persisted_schema_hash != schema_hash
        ):
            findings.append(ArtifactFinding(
                "BOUND_TABLE_RECEIPT_DRIFT",
                f"Persisted table '{spec.table_name}' differs from the "
                "materialization receipt.",
            ))
        counts[f"{schema}.{spec.table_name}"] = int(table.num_rows)
    if findings:
        raise PersistedProjectionError(findings)
    return counts


def validate_persisted_ontology(
    *,
    definition: dict[str, Any],
    manifest: SemanticModelManifest,
    plan: MaterializationPlan,
    workspace_id: str | None = None,
    lakehouse_item_id: str | None = None,
    schema: str | None = None,
    expected_projection_hash: str | None = None,
) -> PersistedSurfaceEvidence:
    """Validate persisted Ontology types, properties, relationships, and bindings."""
    parts = decode_fabric_definition_parts(definition)
    findings = validate_ontology_projection_parts(
        parts,
        manifest,
        plan,
        workspace_id=workspace_id,
        lakehouse_item_id=lakehouse_item_id,
        schema=schema,
    )
    expected_paths = {
        *(
            f"EntityTypes/{entity.ontology_projection.ontology_type_id}/"
            f"definition.json"
            for entity in manifest.entity_types
        ),
        *(
            f"EntityTypes/{entity.ontology_projection.ontology_type_id}/"
            f"DataBindings/{entity.ontology_projection.binding_id}.json"
            for entity in manifest.entity_types
        ),
        *(
            f"RelationshipTypes/"
            f"{relationship.ontology_projection.ontology_rel_type_id}/"
            f"definition.json"
            for relationship in manifest.relationship_types
        ),
        *(
            f"RelationshipTypes/"
            f"{relationship.ontology_projection.ontology_rel_type_id}/"
            f"Contextualizations/"
            f"{relationship.ontology_projection.contextualization_id}.json"
            for relationship in manifest.relationship_types
        ),
    }
    actual_semantic_paths = {
        path
        for path in parts
        if path.startswith(("EntityTypes/", "RelationshipTypes/"))
    }
    if actual_semantic_paths != expected_paths:
        findings.append(ArtifactFinding(
            "ONTOLOGY_PERSISTED_PART_SET_DRIFT",
            "Persisted Ontology semantic parts differ from the sealed "
            f"manifest. Missing={sorted(expected_paths - actual_semantic_paths)}; "
            f"extra={sorted(actual_semantic_paths - expected_paths)}.",
        ))
    projection_hash = persisted_parts_hash(parts)
    if (
        expected_projection_hash is not None
        and projection_hash != expected_projection_hash
    ):
        findings.append(ArtifactFinding(
            "ONTOLOGY_PERSISTED_HASH_DRIFT",
            "Persisted Ontology definition differs from the exact submitted "
            "compiler-owned parts.",
        ))
    if findings:
        raise PersistedProjectionError(findings)
    property_count = sum(
        len(
            payload.get("properties", [])
            if isinstance(payload.get("properties"), list)
            else []
        )
        for path, payload in parts.items()
        if path.startswith("EntityTypes/")
        and path.endswith("/definition.json")
    )
    return PersistedSurfaceEvidence(
        projection_hash=projection_hash,
        definition_counts={
            "parts": len(parts),
            "entity_types": len(manifest.entity_types),
            "properties": property_count,
            "relationship_types": len(manifest.relationship_types),
            "data_bindings": len(manifest.entity_types),
            "contextualizations": len(manifest.relationship_types),
        },
    )


def validate_persisted_graph(
    *,
    definition: dict[str, Any],
    manifest: SemanticModelManifest,
    plan: MaterializationPlan,
    workspace_id: str | None = None,
    lakehouse_item_id: str | None = None,
    schema: str | None = None,
    expected_projection_hash: str | None = None,
) -> PersistedSurfaceEvidence:
    """Validate persisted Graph labels, properties, endpoints, and tables."""
    parts = decode_fabric_definition_parts(definition)
    findings = validate_graph_projection_parts(parts, manifest, plan)
    if workspace_id and lakehouse_item_id and schema:
        findings.extend(validate_graph_source_identity(
            parts=parts,
            plan=plan,
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_item_id,
            schema=schema,
        ))
    graph_type = parts.get("graphType.json", {})
    graph_definition = parts.get("graphDefinition.json", {})
    data_sources = parts.get("dataSources.json", {})
    expected_node_aliases = {
        entity.graph_projection.alias for entity in manifest.entity_types
    }
    expected_edge_aliases = {
        relationship.graph_projection.alias
        for relationship in manifest.relationship_types
    }
    actual_node_aliases = {
        str(item.get("alias"))
        for item in graph_type.get("nodeTypes", [])
        if isinstance(item, dict) and item.get("alias")
    }
    actual_edge_aliases = {
        str(item.get("alias"))
        for item in graph_type.get("edgeTypes", [])
        if isinstance(item, dict) and item.get("alias")
    }
    if actual_node_aliases != expected_node_aliases:
        findings.append(ArtifactFinding(
            "GRAPH_PERSISTED_NODE_SET_DRIFT",
            "Persisted Graph node aliases differ from the manifest.",
        ))
    if actual_edge_aliases != expected_edge_aliases:
        findings.append(ArtifactFinding(
            "GRAPH_PERSISTED_EDGE_SET_DRIFT",
            "Persisted Graph edge aliases differ from the manifest.",
        ))
    projection_hash = persisted_parts_hash(parts)
    if (
        expected_projection_hash is not None
        and projection_hash != expected_projection_hash
    ):
        findings.append(ArtifactFinding(
            "GRAPH_PERSISTED_HASH_DRIFT",
            "Persisted Graph definition differs from the exact submitted "
            "compiler-owned parts.",
        ))
    if findings:
        raise PersistedProjectionError(findings)
    return PersistedSurfaceEvidence(
        projection_hash=projection_hash,
        definition_counts={
            "parts": len(parts),
            "node_types": len(actual_node_aliases),
            "edge_types": len(actual_edge_aliases),
            "node_tables": len(graph_definition.get("nodeTables", [])),
            "edge_tables": len(graph_definition.get("edgeTables", [])),
            "data_sources": len(data_sources.get("dataSources", [])),
        },
    )


def validate_graph_source_identity(
    *,
    parts: dict[str, dict[str, Any]],
    plan: MaterializationPlan,
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str,
) -> list[ArtifactFinding]:
    """Validate exact Graph workspace/Lakehouse/schema/table data sources."""
    findings: list[ArtifactFinding] = []
    payload = parts.get("dataSources.json")
    if not isinstance(payload, dict):
        return [
            ArtifactFinding(
                "GRAPH_DATA_SOURCES_MISSING",
                "Graph definition omits dataSources.json.",
            )
        ]
    references = {
        str(reference.get("name")): reference.get("item")
        for reference in payload.get("itemReferences", [])
        if isinstance(reference, dict) and reference.get("name")
    }
    expected_paths = {
        f"Tables/{schema}/{table.table_name}"
        for table in [*plan.entity_tables, *plan.relationship_tables]
    }
    actual_paths: set[str] = set()
    for source in payload.get("dataSources", []):
        if not isinstance(source, dict):
            continue
        properties = source.get("properties")
        if not isinstance(properties, dict):
            findings.append(ArtifactFinding(
                "GRAPH_DATA_SOURCE_INVALID",
                f"Graph data source {source.get('name')!r} has no properties.",
            ))
            continue
        reference_name = str(properties.get("referenceName") or "")
        item = references.get(reference_name)
        if not isinstance(item, dict) or (
            str(item.get("workspaceId") or "") != workspace_id
            or str(item.get("itemId") or "") != lakehouse_item_id
        ):
            findings.append(ArtifactFinding(
                "GRAPH_DATA_SOURCE_ITEM_DRIFT",
                f"Graph data source {source.get('name')!r} is not bound to "
                "the authoritative workspace/Lakehouse.",
            ))
        actual_paths.add(str(properties.get("path") or ""))
    if actual_paths != expected_paths:
        findings.append(ArtifactFinding(
            "GRAPH_DATA_SOURCE_PATH_DRIFT",
            "Graph data-source table paths differ from the sealed "
            f"materialization plan. Missing={sorted(expected_paths - actual_paths)}; "
            f"extra={sorted(actual_paths - expected_paths)}.",
        ))
    return findings


def validate_graph_query_readiness(
    *,
    manifest: SemanticModelManifest,
    plan: MaterializationPlan,
    workspace_id: str,
    graph_model_id: str,
    gql_client: GQLReadinessClient,
    canvas_visibility: str = "not_observed",
) -> QueryReadiness:
    """Run count-only and typed-path GQL against the persisted Graph Model."""
    if canvas_visibility not in {
        "not_observed",
        "visible",
        "not_visible",
    }:
        raise ValueError(
            f"Unsupported canvas visibility state: {canvas_visibility!r}."
        )
    availability = {
        item.semantic_id: item for item in plan.data_availability
    }
    findings: list[ArtifactFinding] = []
    notes: list[str] = []
    counts_by_semantic_id: dict[str, int] = {}
    node_total = 0
    edge_total = 0

    for entity in manifest.entity_types:
        query = GQLQueryBuilder.node_count(
            entity.graph_projection.label
        )
        try:
            response = gql_client.execute_query(
                workspace_id,
                graph_model_id,
                query,
            )
        except Exception as exc:
            findings.append(ArtifactFinding(
                "GRAPH_NODE_COUNT_QUERY_FAILED",
                f"Node count failed for '{entity.semantic_id}': {exc}.",
            ))
            continue
        code = _gql_status_code(response)
        count = _gql_extract_count(response)
        if not _gql_status_ok(code) or count is None:
            findings.append(ArtifactFinding(
                "GRAPH_NODE_COUNT_QUERY_INVALID",
                f"Node count for '{entity.semantic_id}' returned "
                f"status={code!r}, count={count!r}.",
            ))
            continue
        counts_by_semantic_id[entity.semantic_id] = count
        node_total += count

    for relationship in manifest.relationship_types:
        query = GQLQueryBuilder.edge_count(
            relationship.graph_projection.source_label,
            relationship.graph_projection.label,
            relationship.graph_projection.target_label,
        )
        try:
            response = gql_client.execute_query(
                workspace_id,
                graph_model_id,
                query,
            )
        except Exception as exc:
            findings.append(ArtifactFinding(
                "GRAPH_EDGE_COUNT_QUERY_FAILED",
                f"Edge count failed for '{relationship.semantic_id}': {exc}.",
            ))
            continue
        code = _gql_status_code(response)
        count = _gql_extract_count(response)
        if not _gql_status_ok(code) or count is None:
            findings.append(ArtifactFinding(
                "GRAPH_EDGE_COUNT_QUERY_INVALID",
                f"Edge count for '{relationship.semantic_id}' returned "
                f"status={code!r}, count={count!r}.",
            ))
            continue
        counts_by_semantic_id[relationship.semantic_id] = count
        edge_total += count
        relationship_availability = availability.get(
            relationship.semantic_id
        )
        if (
            count == 0
            and relationship_availability is not None
            and relationship_availability.observed_rows == 0
            and relationship_availability.required_rows == 0
        ):
            notes.append(
                "typed_path_skipped_empty="
                f"{relationship.semantic_id}"
            )
            continue

        typed_query = GQLQueryBuilder.typed_path_sample(
            relationship.graph_projection.source_label,
            relationship.graph_projection.label,
            relationship.graph_projection.target_label,
            src_id_property=entity_table_by_id[
                relationship.source_type_id
            ].entity_id_column,
            evidence_property=(
                relationship_table_by_id[
                    relationship.semantic_id
                ].evidence_column
                or "evidence_id"
            ),
            dst_id_property=entity_table_by_id[
                relationship.target_type_id
            ].entity_id_column,
        )
        try:
            typed_response = gql_client.execute_query(
                workspace_id,
                graph_model_id,
                typed_query,
            )
        except Exception as exc:
            findings.append(ArtifactFinding(
                "GRAPH_TYPED_PATH_QUERY_FAILED",
                f"Typed path failed for '{relationship.semantic_id}': {exc}.",
            ))
            continue
        typed_code = _gql_status_code(typed_response)
        typed_data = typed_response.get("result", {}).get("data")
        if (
            not _gql_status_ok(typed_code)
            or not isinstance(typed_data, list)
            or not typed_data
        ):
            findings.append(ArtifactFinding(
                "GRAPH_TYPED_PATH_QUERY_INVALID",
                f"Typed path for '{relationship.semantic_id}' returned "
                f"status={typed_code!r} without a persisted path row.",
            ))

    if not manifest.relationship_types:
        findings.append(ArtifactFinding(
            "GRAPH_TYPED_PATH_UNAVAILABLE",
            "Persisted Graph readiness requires at least one relationship type.",
        ))
    if node_total <= 0:
        findings.append(ArtifactFinding(
            "GRAPH_NODE_DATA_EMPTY",
            "Persisted Graph count queries returned zero total nodes.",
        ))
    expects_nonempty_relationships = any(
        availability.get(relationship.semantic_id) is None
        or availability[relationship.semantic_id].observed_rows > 0
        or availability[relationship.semantic_id].required_rows > 0
        for relationship in manifest.relationship_types
    )
    if expects_nonempty_relationships and edge_total <= 0:
        findings.append(ArtifactFinding(
            "GRAPH_EDGE_DATA_EMPTY",
            "Persisted Graph count queries returned zero total edges.",
        ))
    required_ok = True
    for semantic_id, item in availability.items():
        count = counts_by_semantic_id.get(semantic_id)
        if item.required_rows > 0 and (
            count is None or count < item.required_rows
        ):
            required_ok = False
            findings.append(ArtifactFinding(
                "GRAPH_REQUIRED_COMPETENCY_EMPTY",
                f"'{semantic_id}' requires {item.required_rows} row(s), "
                f"persisted Graph count is {count!r}.",
            ))
        if item.observed_rows > 0 and count == 0:
            required_ok = False
            findings.append(ArtifactFinding(
                "GRAPH_OBSERVED_DATA_EMPTY",
                f"'{semantic_id}' has {item.observed_rows} observed row(s), "
                "but the persisted Graph count is zero.",
            ))
    notes.append(f"canvas_visibility={canvas_visibility}")
    if canvas_visibility == "not_visible":
        notes.append(
            "Graph backend/schema/GQL passed while the Fabric canvas was "
            "operator-observed as not visible."
        )
    if findings:
        raise PersistedProjectionError(findings)
    return QueryReadiness(
        count_query_passed=True,
        typed_path_query_passed=True,
        nonzero_required_competencies=required_ok,
        gql_node_count=node_total,
        gql_edge_count=edge_total,
        canvas_visibility=canvas_visibility,
        notes=notes,
        observed_relationship_rows={
            semantic_id: count
            for semantic_id, count in counts_by_semantic_id.items()
            if any(
                r.semantic_id == semantic_id
                for r in manifest.relationship_types
            )
        },
    )


def build_persisted_projection_receipt(
    *,
    manifest: SemanticModelManifest,
    ontology_item_id: str,
    ontology_evidence: PersistedSurfaceEvidence,
    graph_model_id: str,
    graph_evidence: PersistedSurfaceEvidence,
    materialization_receipt: MaterializationReceipt | None = None,
    bound_table_counts: dict[str, int],
    query_readiness: QueryReadiness,
    validated_at_utc: str | None = None,
) -> PersistedProjectionReceipt:
    """Seal independently observed H3 evidence into the downstream authority."""
    return PersistedProjectionReceipt(
        semantic_contract_hash=(
            manifest.semantic_contract_hash
            if materialization_receipt is not None
            else ""
        ),
        projection_receipt_hash=(
            materialization_receipt.projection_receipt_hash
            if materialization_receipt is not None
            else ""
        ),
        semantic_model_manifest_hash=manifest.manifest_hash,
        semantic_crosswalk_hash=(
            materialization_receipt.semantic_crosswalk_hash
            if materialization_receipt is not None
            else ""
        ),
        materialization_plan_hash=(
            materialization_receipt.materialization_plan_hash
            if materialization_receipt is not None
            else ""
        ),
        materialization_receipt=materialization_receipt,
        ontology_item_id=ontology_item_id,
        ontology_persisted_projection_hash=(
            ontology_evidence.projection_hash
        ),
        graph_model_id=graph_model_id,
        graph_persisted_projection_hash=graph_evidence.projection_hash,
        ontology_definition_counts=ontology_evidence.definition_counts,
        graph_definition_counts=graph_evidence.definition_counts,
        bound_table_counts=bound_table_counts,
        query_readiness=query_readiness,
        validated_at_utc=validated_at_utc or _utc_now(),
    )
