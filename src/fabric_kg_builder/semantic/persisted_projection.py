"""Persisted Ontology and Graph projection readiness for SPEC-008A H3."""

from __future__ import annotations

import base64
import hashlib
import json
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
    PersistedProjectionReceipt,
    QueryReadiness,
    SemanticModelManifest,
)
from fabric_kg_builder.semantic.source_tables import (
    resolve_semantic_source_parquet,
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


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
    availability = {
        item.semantic_id: item for item in plan.data_availability
    }
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

    import pyarrow.parquet as pq  # type: ignore[import]

    source_cache: dict[Path, Any] = {}
    prepared: dict[str, tuple[Any, MaterializedTableEvidence]] = {}
    findings: list[ArtifactFinding] = []
    for spec in specs:
        try:
            source_path = resolve_source_parquet(
                parquet_dir,
                str(spec.source_table_name or ""),
            )
        except PersistedProjectionError as exc:
            findings.extend(exc.findings)
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
        expected_names = [column.column_name for column in spec.columns]
        missing = sorted(set(expected_names) - set(sliced.schema.names))
        if missing:
            findings.append(ArtifactFinding(
                "MATERIALIZATION_SOURCE_COLUMNS_MISSING",
                f"Source '{source_path.name}' for '{spec.semantic_id}' "
                f"omits {missing}.",
            ))
            continue
        projected = sliced.select(expected_names)
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
        prepared[spec.table_name] = (
            projected,
            MaterializedTableEvidence(
                semantic_id=spec.semantic_id,
                table_name=spec.table_name,
                source_path=str(source_path),
                row_count=projected.num_rows,
                columns=tuple(expected_names),
                status="prepared",
            ),
        )
    if findings:
        raise PersistedProjectionError(findings)

    if table_writer is None:
        from deltalake import write_deltalake  # type: ignore[import]

        if token_provider is None:
            from azure.identity import DefaultAzureCredential  # type: ignore[import]

            credential = DefaultAzureCredential()
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


def validate_bound_tables(
    *,
    plan: MaterializationPlan,
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str,
    table_reader: BoundTableReader,
) -> dict[str, int]:
    """Read persisted bound tables and validate exact schema, keys, and counts."""
    availability = {
        item.semantic_id: item for item in plan.data_availability
    }
    findings: list[ArtifactFinding] = []
    counts: dict[str, int] = {}
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
        projection_hash=persisted_parts_hash(parts),
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
) -> PersistedSurfaceEvidence:
    """Validate persisted Graph labels, properties, endpoints, and tables."""
    parts = decode_fabric_definition_parts(definition)
    findings = validate_graph_projection_parts(parts, manifest, plan)
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
    if findings:
        raise PersistedProjectionError(findings)
    return PersistedSurfaceEvidence(
        projection_hash=persisted_parts_hash(parts),
        definition_counts={
            "parts": len(parts),
            "node_types": len(actual_node_aliases),
            "edge_types": len(actual_edge_aliases),
            "node_tables": len(graph_definition.get("nodeTables", [])),
            "edge_tables": len(graph_definition.get("edgeTables", [])),
            "data_sources": len(data_sources.get("dataSources", [])),
        },
    )


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
    )


def build_persisted_projection_receipt(
    *,
    manifest: SemanticModelManifest,
    ontology_item_id: str,
    ontology_evidence: PersistedSurfaceEvidence,
    graph_model_id: str,
    graph_evidence: PersistedSurfaceEvidence,
    bound_table_counts: dict[str, int],
    query_readiness: QueryReadiness,
    validated_at_utc: str | None = None,
) -> PersistedProjectionReceipt:
    """Seal independently observed H3 evidence into the downstream authority."""
    return PersistedProjectionReceipt(
        semantic_model_manifest_hash=manifest.manifest_hash,
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
