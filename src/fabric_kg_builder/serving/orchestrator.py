"""serving.orchestrator — idempotent full redeploy orchestrator.

M6 SRV-011

Design
------
``deploy_all()`` executes an idempotent full serving deployment.  Callers
MUST supply an explicit ``transports`` object — there is no fallback to fakes.
Use ``FakeOrchestratorTransports`` in tests only.

Pre-cutover gate sequence (required before alias mutation):
  1. create/get index
  2. validate stored schema (when reused — genuine, not circular)
  3. upload documents
  4. count_probe            — must return ok (malformed count = failure)
  5. text_query_probe       — index must be queryable
  6. citation_sample_probe  — required lineage fields must be present
  All six must pass; on gate failure the alias is NOT touched.

Post-cutover:
  7. alias cutover
  8. post-cutover verification (alias resolves to correct index)
  9. rollback on post-cutover failure; surface both failures

Completeness:
  overall_ok = no errors AND create ok AND competency ok AND lineage ok
  (partial competency/lineage failures propagate — NOT silently ignored)

Dry-run mode:
  Pass ``dry_run=True`` to plan actions without making any modifying calls.

CLI wiring:
  Exposed via ``deploy-serving`` subcommand in cli/deploy_cmd.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fabric_kg_builder.lineage.registry import record_deployment
from fabric_kg_builder.model.schemas import DeploymentRow
from fabric_kg_builder.serving.competency import (
    CompetencyResult,
    CompetencyVerifier,
    FakeLakehouseClient,
)
from fabric_kg_builder.serving.index_version import (
    compute_index_fingerprint,
    physical_index_name,
    stable_alias,
)
from fabric_kg_builder.serving.lineage_verifier import (
    FakeSearchSampler,
    LineageVerifier,
    VerificationReport,
)
from fabric_kg_builder.serving.release_manager import (
    CANONICAL_LINEAGE_FIELDS,
    FakeSearchTransport,
    ReleaseManager,
    ReleaseResult,
)

logger = logging.getLogger(__name__)

_MAX_SEARCH_UPLOAD_BYTES = 14 * 1024 * 1024
_MAX_SEARCH_UPLOAD_ACTIONS = 1_000


def _iter_search_upload_batches(
    actions: list[dict[str, Any]],
    *,
    max_payload_bytes: int = _MAX_SEARCH_UPLOAD_BYTES,
    max_actions: int = _MAX_SEARCH_UPLOAD_ACTIONS,
):
    """Yield Azure AI Search action batches bounded by count and JSON size."""
    payload_overhead = len(b'{"value":[]}')
    batch: list[dict[str, Any]] = []
    batch_bytes = payload_overhead

    for action in actions:
        action_bytes = len(json.dumps(action, ensure_ascii=False).encode("utf-8"))
        if action_bytes + payload_overhead > max_payload_bytes:
            raise ValueError(
                "A single Search document exceeds the maximum upload payload size."
            )

        separator_bytes = 1 if batch else 0
        if batch and (
            len(batch) >= max_actions
            or batch_bytes + separator_bytes + action_bytes > max_payload_bytes
        ):
            yield batch
            batch = []
            batch_bytes = payload_overhead
            separator_bytes = 0

        batch.append(action)
        batch_bytes += separator_bytes + action_bytes

    if batch:
        yield batch


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorConfig:
    """Configuration for a full serving deployment.

    Attributes
    ----------
    workspace_id / lakehouse_item_id / search_endpoint / base_index_name:
        Target resource coordinates.
    schema_dict:
        Index schema dict used for fingerprint computation.
    embedding_model / dimensions:
        Embedding model and dimension count (immutable per physical index).
    run_id / environment:
        Lineage tracking coordinates.
    parquet_dir:
        Directory with Parquet files.  Optional — skipped when None.
    docs:
        Search documents to upload.  Empty list → schema-only deploy.
    schema:
        Lakehouse schema name (default "dbo").
    tables:
        Lakehouse tables to deploy.  Defaults to all v2 tables.
    deployment_store:
        Caller-owned list that receives DeploymentRow dicts.  When provided,
        records are appended so the caller can persist them to a manifest/store.
        When None a local list is used and the rows are returned in the result.
    ontology_item_id:
        Ontology item ID for lineage/competency verification (optional).
    """

    workspace_id: str
    lakehouse_item_id: str
    search_endpoint: str
    base_index_name: str
    schema_dict: dict[str, Any]
    embedding_model: str
    dimensions: int
    run_id: str
    environment: str
    parquet_dir: Optional[Path] = None
    deploy_lakehouse: bool = True
    docs: list[dict[str, Any]] = field(default_factory=list)
    schema: str = "dbo"
    tables: Optional[list[str]] = None
    deployment_store: Optional[list] = None
    ontology_item_id: str = ""
    graph_model_name: str = ""
    graph_model_id: str = ""
    graph_model_parts: list[dict[str, Any]] = field(default_factory=list)
    graph_entity_types: list[str] = field(default_factory=list)
    graph_relationship_pairs: list[dict[str, Any]] = field(default_factory=list)
    graph_preview_acknowledged: bool = False
    graph_lineage_label: str = ""
    graph_lineage_fields: list[str] = field(default_factory=lambda: [
        "project_id",
        "asset_id",
        "asset_version_id",
        "run_id",
        "source_locator_json",
        "schema_version",
    ])


_GRAPH_LINEAGE_PROPERTY_PRIORITY = (
    "source_file_id",
    "asset_version_id",
    "asset_id",
    "run_id",
    "source_locator_json",
    "project_id",
    "schema_version",
    "evidence_ids_json",
    "citation_json",
    "entity_id",
)


def _select_graph_lineage_probe(
    label_catalog: dict[str, Any],
) -> tuple[str, list[str]]:
    """Select a node label and only its contract-declared lineage properties."""
    nodes = label_catalog.get("nodes", [])
    if not isinstance(nodes, list):
        return "", []
    candidates: list[tuple[int, int, str, list[str]]] = []
    for position, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        label = str(node.get("graph_label") or "").strip()
        raw_properties = node.get("properties", [])
        if not label or not isinstance(raw_properties, list):
            continue
        declared = {
            str(value).strip()
            for value in raw_properties
            if str(value).strip()
        }
        fields = [
            field
            for field in _GRAPH_LINEAGE_PROPERTY_PRIORITY
            if field in declared
        ]
        if fields:
            candidates.append((
                _GRAPH_LINEAGE_PROPERTY_PRIORITY.index(fields[0]),
                position,
                label,
                fields,
            ))
    if not candidates:
        return "", []
    _, _, label, fields = min(candidates)
    return label, fields


# ---------------------------------------------------------------------------
# Transports bundle (injectable)
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorTransports:
    """All injectable transports/clients for the orchestrator.

    For live deployments supply real implementations; for tests supply fakes.
    All fields are required — ``deploy_all()`` does NOT default any of them.
    """

    search_transport: Any
    lakehouse_client: Any
    search_sampler: Any
    token_provider: Any = None
    ontology_sampler: Any = None
    graph_model_transport: Any = None
    fabric_token_provider: Any = None
    gql_client: Any = None


class FakeOrchestratorTransports:
    """All-fake transport bundle for tests only — no cloud calls.

    DO NOT pass this as a production default.  ``deploy_all()`` raises if
    no transports are provided.
    """

    def __init__(self) -> None:
        self.search_transport = FakeSearchTransport()
        self.lakehouse_client = FakeLakehouseClient(entity_count=10, relationship_count=5)
        self.search_sampler = FakeSearchSampler()
        self.token_provider = None
        self.ontology_sampler = None

    def set_entity_count(self, n: int) -> None:
        self.lakehouse_client = FakeLakehouseClient(
            entity_count=n,
            relationship_count=max(1, n // 2),
        )

    def set_search_docs(self, docs: list[dict[str, Any]]) -> None:
        self.search_sampler = FakeSearchSampler(docs=docs)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorResult:
    """Typed result of a full serving deployment.

    ``ok`` is True iff ALL required steps succeeded:
      - index create/reuse ok
      - all pre-cutover gates ok (count, text_query, citation)
      - alias cutover ok
      - competency ok (when checked)
      - lineage ok (when checked)
      - no accumulated errors

    ``pre_cutover_gates_ok`` is False when any gate prevented alias mutation.
    ``rollback_result`` is populated when post-cutover verification triggered rollback.
    """

    ok: bool
    physical_index_name: str
    alias: str
    index_action: str = "unknown"
    fingerprint: str = ""
    docs_pushed: int = 0
    pre_cutover_gates_ok: bool = False
    lakehouse_tables: dict[str, str] = field(default_factory=dict)
    competency: Optional[CompetencyResult] = None
    lineage_report: Optional[VerificationReport] = None
    deployment_rows: list[DeploymentRow] = field(default_factory=list)
    rollback_result: Optional[ReleaseResult] = None
    graph_model_id: str = ""
    graph_model_action: str = ""
    errors: list[str] = field(default_factory=list)
    partial_failures: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def deploy_all(
    cfg: OrchestratorConfig,
    transports: "OrchestratorTransports | FakeOrchestratorTransports",
    *,
    dry_run: bool = False,
) -> OrchestratorResult:
    """Execute an idempotent full serving deployment.

    Parameters
    ----------
    cfg:
        Deployment configuration.
    transports:
        Injectable transport bundle.  This is a REQUIRED argument — there is
        no fallback to fakes.  Pass ``FakeOrchestratorTransports()`` in tests.
    dry_run:
        When True, plan actions and validate config but skip all modifying
        calls (no index PUT, no alias change, no OneLake write).

    Returns
    -------
    OrchestratorResult with ``ok=True`` iff all critical steps passed.
    """
    # Validate transports are present.
    if transports is None:
        raise ValueError(
            "deploy_all() requires explicit transports. "
            "Pass FakeOrchestratorTransports() in tests or OrchestratorTransports with real clients."
        )

    tp = transports
    errors: list[str] = []
    partial_failures: list[dict[str, Any]] = []
    dep_store = cfg.deployment_store if cfg.deployment_store is not None else []

    # ── Step 1: Compute versioned index name ────────────────────────────────
    fingerprint = compute_index_fingerprint(cfg.schema_dict, cfg.embedding_model, cfg.dimensions)
    phys_name = physical_index_name(cfg.base_index_name, fingerprint)
    alias = stable_alias(cfg.base_index_name)
    logger.info("[orchestrator] fingerprint=%s phys=%s alias=%s dry_run=%s",
                fingerprint, phys_name, alias, dry_run)

    # ── Step 2: Deploy OneLake tables ────────────────────────────────────────
    lakehouse_results: dict[str, str] = {}
    if cfg.deploy_lakehouse and cfg.parquet_dir is not None and not dry_run:
        from fabric_kg_builder.deploy.onelake_writer import (
            LAKEHOUSE_TABLE_PROJECTION,
            deploy_parquet_to_onelake,
        )
        tables = cfg.tables or list(LAKEHOUSE_TABLE_PROJECTION.keys())
        try:
            lakehouse_results = deploy_parquet_to_onelake(
                parquet_dir=cfg.parquet_dir,
                workspace_id=cfg.workspace_id,
                lakehouse_item_id=cfg.lakehouse_item_id,
                schema=cfg.schema,
                tables=tables,
                projection=LAKEHOUSE_TABLE_PROJECTION,
                mock=False,
            )
        except Exception as exc:
            err = f"OneLake deploy failed: {exc}"
            errors.append(err)
            partial_failures.append({"step": "onelake", "error": str(exc)})
            logger.error("[orchestrator] %s", err)
        for table_name, status in lakehouse_results.items():
            if str(status).startswith("error"):
                err = f"OneLake deploy failed for {table_name}: {status}"
                errors.append(err)
                partial_failures.append({
                    "step": "onelake",
                    "table": table_name,
                    "error": str(status),
                })
    elif cfg.deploy_lakehouse and dry_run:
        from fabric_kg_builder.deploy.onelake_writer import LAKEHOUSE_TABLE_PROJECTION
        from fabric_kg_builder.deploy.onelake_writer import STATUS_PLANNED
        tables = cfg.tables or list(LAKEHOUSE_TABLE_PROJECTION.keys())
        lakehouse_results = {t: STATUS_PLANNED for t in tables if t in LAKEHOUSE_TABLE_PROJECTION}

    # ── Step 3: Deploy the GraphModel definition ────────────────────────────
    graph_model_id = ""
    graph_model_action = ""
    if cfg.graph_model_parts:
        if not cfg.graph_preview_acknowledged:
            errors.append(
                "GraphModel deployment requires explicit preview acknowledgement."
            )
        elif dry_run:
            graph_model_action = "planned"
        elif errors:
            graph_model_action = "blocked-by-onelake"
        else:
            from fabric_kg_builder.serving.graph_model import create_or_get_graph_model

            try:
                graph_result = create_or_get_graph_model(
                    workspace_id=cfg.workspace_id,
                    name=cfg.graph_model_name or "kg_graph_model",
                    parts=cfg.graph_model_parts,
                    graph_model_id=cfg.graph_model_id or None,
                    token_provider=getattr(tp, "fabric_token_provider", None),
                    transport=getattr(tp, "graph_model_transport", None),
                )
                graph_model_id = str(graph_result.get("item_id") or "")
                graph_model_action = str(graph_result.get("action") or "unknown")
                if not graph_model_id:
                    note = graph_result.get("note", "GraphModel returned no item ID")
                    errors.append(str(note))
                    partial_failures.append({
                        "step": "graph_model",
                        "action": graph_model_action,
                        "error": str(note),
                    })
            except Exception as exc:
                err = f"GraphModel deployment failed: {exc}"
                errors.append(err)
                partial_failures.append({
                    "step": "graph_model",
                    "error": str(exc),
                })

    if errors:
        return OrchestratorResult(
            ok=False,
            physical_index_name=phys_name,
            alias=alias,
            fingerprint=fingerprint,
            lakehouse_tables=lakehouse_results,
            graph_model_id=graph_model_id,
            graph_model_action=graph_model_action,
            errors=errors,
            partial_failures=partial_failures,
        )

    # ── Step 4: Get or create versioned index ───────────────────────────────
    search_transport = getattr(tp, "search_transport", None)
    token_provider = getattr(tp, "token_provider", None)
    mgr = ReleaseManager(
        endpoint=cfg.search_endpoint,
        token_provider=token_provider,
        transport=search_transport,
    )

    create_result: ReleaseResult = mgr.get_or_create_index(phys_name, cfg.schema_dict)
    index_action = create_result.metadata.get("action", "unknown")

    if not create_result.ok:
        errors.extend(create_result.errors)
        partial_failures.extend(create_result.partial_failures)
        # Cannot proceed without the index
        return OrchestratorResult(
            ok=False, physical_index_name=phys_name, alias=alias,
            index_action=index_action, fingerprint=fingerprint,
            lakehouse_tables=lakehouse_results,
            errors=errors, partial_failures=partial_failures,
        )

    # ── Step 5: Validate stored schema when index was reused ────────────────
    if index_action == "reused" and not dry_run:
        stored_schema = create_result.metadata.get("stored_schema", {})
        val_result = mgr.validate_stored_schema(
            phys_name=phys_name,
            expected_schema=cfg.schema_dict,
            expected_embedding_model=cfg.embedding_model,
            expected_dimensions=cfg.dimensions,
            stored_schema=stored_schema,
        )
        if not val_result.ok:
            errors.extend(val_result.errors)
            partial_failures.extend(val_result.partial_failures)
            # Schema mismatch → abort before upload
            return OrchestratorResult(
                ok=False, physical_index_name=phys_name, alias=alias,
                index_action=index_action, fingerprint=fingerprint,
                lakehouse_tables=lakehouse_results,
                errors=errors, partial_failures=partial_failures,
            )

    # ── Step 6: Upload documents ─────────────────────────────────────────────
    docs_pushed = 0
    if cfg.docs and not dry_run:
        from fabric_kg_builder.deploy.search_deployer import _API_VERSION as _SAPI
        upload_url = (
            f"{cfg.search_endpoint}/indexes/{phys_name}/docs/index"
            f"?api-version={_SAPI}&allowUnsafeKeys=true"
        )
        actions = [{"@search.action": "mergeOrUpload", **doc} for doc in cfg.docs]
        hdr: dict[str, str] = {"Content-Type": "application/json"}
        if token_provider:
            tok = token_provider()
            hdr["Authorization"] = f"Bearer {tok}"
        try:
            batches = _iter_search_upload_batches(actions)
            for batch in batches:
                upload_resp = search_transport.post(upload_url, hdr, {"value": batch})
                if not upload_resp or not upload_resp.ok:
                    status = upload_resp.status_code if upload_resp else "no response"
                    err = f"Document upload failed: HTTP {status}"
                    errors.append(err)
                    partial_failures.append({"step": "doc_upload", "error": err})
                    break

                upload_rows = (upload_resp.body or {}).get("value", [])
                failed_rows = [
                    row for row in upload_rows
                    if isinstance(row, dict) and not row.get("status", False)
                ]
                if failed_rows:
                    err = (
                        f"Document upload reported {len(failed_rows)} failed action(s)"
                    )
                    errors.append(err)
                    partial_failures.append({
                        "step": "doc_upload",
                        "failed_actions": failed_rows,
                    })
                    break
                docs_pushed += len(batch)
        except ValueError as exc:
            err = f"Document upload failed: {exc}"
            errors.append(err)
            partial_failures.append({"step": "doc_upload", "error": err})

        if errors:
            return OrchestratorResult(
                ok=False, physical_index_name=phys_name, alias=alias,
                index_action=index_action, fingerprint=fingerprint,
                docs_pushed=docs_pushed, pre_cutover_gates_ok=False,
                lakehouse_tables=lakehouse_results,
                errors=errors, partial_failures=partial_failures,
            )

    # ── Step 7: Pre-cutover gates ────────────────────────────────────────────
    #   count_probe → text_query_probe → citation_sample_probe
    #   ALL must pass before alias is touched.
    gate_errors: list[str] = []
    if not dry_run:
        # count_probe: must succeed (malformed body = failure per release_manager)
        cp = mgr.count_probe(phys_name)
        if not cp.ok:
            gate_errors.extend(cp.errors)
            partial_failures.extend(cp.partial_failures)

        # text_query_probe: index must be queryable
        if not gate_errors:
            tq = mgr.text_query_probe(phys_name)
            if not tq.ok:
                gate_errors.extend(tq.errors)
                partial_failures.extend(tq.partial_failures)

        # citation_sample_probe: required when documents are present
        if not gate_errors and cfg.docs:
            cit = mgr.citation_sample_probe(phys_name)
            if not cit.ok:
                gate_errors.extend(cit.errors)
                partial_failures.extend(cit.partial_failures)

    pre_cutover_gates_ok = not gate_errors
    if gate_errors:
        errors.extend(gate_errors)
        # Alias is NOT mutated when any gate fails
        logger.error("[orchestrator] Pre-cutover gates failed — alias not mutated: %s", gate_errors)
        return OrchestratorResult(
            ok=False, physical_index_name=phys_name, alias=alias,
            index_action=index_action, fingerprint=fingerprint,
            docs_pushed=docs_pushed,
            pre_cutover_gates_ok=False,
            lakehouse_tables=lakehouse_results,
            errors=errors, partial_failures=partial_failures,
        )

    # ── Step 8: Atomic alias cutover ─────────────────────────────────────────
    alias_result: Optional[ReleaseResult] = None
    rollback_result: Optional[ReleaseResult] = None
    if not dry_run:
        alias_result = mgr.atomic_alias_cutover(alias, phys_name)
        if not alias_result.ok:
            errors.extend(alias_result.errors)
            partial_failures.extend(alias_result.partial_failures)
        else:
            # ── Step 9: Post-cutover verification ────────────────────────────
            # Verify alias now resolves to phys_name
            from fabric_kg_builder.serving.release_manager import _Response as _R
            headers = mgr._headers()
            alias_check = search_transport.get(
                f"{cfg.search_endpoint}/aliases/{alias}?api-version={mgr._v()}",
                headers,
            )
            if alias_check.status_code == 200:
                resolved = (alias_check.body or {}).get("indexes", [None])[0]
                if resolved != phys_name:
                    post_err = (
                        f"Post-cutover verification failed: alias '{alias}' "
                        f"resolves to '{resolved}', expected '{phys_name}'."
                    )
                    logger.error("[orchestrator] %s — rolling back", post_err)
                    prev = alias_result.metadata.get("previous_target")
                    if prev:
                        rollback_result = mgr.rollback(alias, prev)
                        rb_status = "ok" if rollback_result.ok else "failed"
                    else:
                        rollback_result = None
                        rb_status = "no-prev-target"
                    errors.append(post_err)
                    partial_failures.append({
                        "step": "post_cutover_verify",
                        "error": post_err,
                        "rollback": rb_status,
                    })

    # ── Step 10: Competency verification ────────────────────────────────────
    lakehouse_client = getattr(tp, "lakehouse_client", None)
    competency_result: Optional[CompetencyResult] = None
    if lakehouse_client is not None:
        gql_client = getattr(tp, "gql_client", None)
        verifier = CompetencyVerifier(
            client=lakehouse_client,
            gql_client=gql_client,
            gql_beta_acknowledged=cfg.graph_preview_acknowledged,
        )
        competency_result = verifier.verify_all(
            cfg.workspace_id,
            cfg.lakehouse_item_id,
            cfg.schema,
            ontology_item_id=cfg.ontology_item_id,
            graph_model_id=graph_model_id,
            gql_node_labels=cfg.graph_entity_types,
            gql_node_min_count=0,
            gql_relationship_pairs=cfg.graph_relationship_pairs,
            gql_lineage_label=(
                cfg.graph_lineage_label
                or (cfg.graph_entity_types[0] if cfg.graph_entity_types else "")
            ),
            gql_lineage_fields=cfg.graph_lineage_fields,
        )
        if not competency_result.ok:
            partial_failures.append({
                "step": "competency",
                "errors": competency_result.errors,
                "partial_failures": competency_result.partial_failures,
            })

    # ── Step 11: Lineage verification ────────────────────────────────────────
    search_sampler = getattr(tp, "search_sampler", None)
    lineage_report: Optional[VerificationReport] = None
    if search_sampler is not None:
        # Load tables from parquet_dir if available so lineage has data to trace
        tables_for_lineage: dict[str, list] = {}
        if cfg.parquet_dir is not None:
            _pd = Path(cfg.parquet_dir)
            if _pd.exists():
                try:
                    import pyarrow.parquet as _pq  # type: ignore[import]
                    for _pf in _pd.glob("*.parquet"):
                        tables_for_lineage[_pf.stem] = _pq.read_table(str(_pf)).to_pylist()
                except ImportError:
                    pass

        ontology_sampler = getattr(tp, "ontology_sampler", None)
        lv = LineageVerifier(
            sampler=search_sampler,
            doc_id_field="chunk_id",
            table_name="chunks",
            ontology_sampler=ontology_sampler,
            workspace_id=cfg.workspace_id,
            ontology_item_id=cfg.ontology_item_id,
            ontology_type_name="Entity",
        )
        lineage_report = lv.verify(
            tables=tables_for_lineage,
            index_name=phys_name,
            sample_size=5,
        )
        if not lineage_report.ok and lineage_report.sample_count > 0:
            partial_failures.append({
                "step": "lineage",
                "resolved": lineage_report.resolved_count,
                "missing": lineage_report.missing_count,
                "broken": lineage_report.broken_count,
            })

    # ── Step 12: Persist deployment locators ─────────────────────────────────
    search_loc = f"search://{cfg.search_endpoint}/{phys_name}"
    dep_row = record_deployment(
        dep_store,
        run_id=cfg.run_id,
        environment=cfg.environment,
        artifact_type="search_index",
        artifact_version=fingerprint,
        target_resource_id=cfg.search_endpoint,
        target_name=phys_name,
        target_record_locator=search_loc,
        status="succeeded" if not errors else "partial",
        record_ids=[phys_name, alias],
    )
    dep_rows: list[DeploymentRow] = [dep_row]

    if graph_model_id:
        graph_dep = record_deployment(
            dep_store,
            run_id=cfg.run_id,
            environment=cfg.environment,
            artifact_type="fabric_graph_model",
            artifact_version=fingerprint,
            target_resource_id=cfg.workspace_id,
            target_name=cfg.graph_model_name or "kg_graph_model",
            target_record_locator=(
                f"fabric://workspaces/{cfg.workspace_id}/graphModels/{graph_model_id}"
            ),
            status="succeeded" if competency_result and competency_result.ok else "partial",
            record_ids=[graph_model_id],
        )
        dep_rows.append(graph_dep)

    alias_loc = f"search://alias/{alias}->{phys_name}"
    alias_dep = record_deployment(
        dep_store,
        run_id=cfg.run_id,
        environment=cfg.environment,
        artifact_type="search_alias",
        artifact_version=fingerprint,
        target_resource_id=cfg.search_endpoint,
        target_name=alias,
        target_record_locator=alias_loc,
        status="succeeded" if (alias_result and alias_result.ok) else "partial",
    )
    dep_rows.append(alias_dep)

    # overall_ok includes competency and lineage — partial failures propagate
    comp_ok = competency_result.ok if competency_result is not None else True
    lin_ok = (
        True
        if not cfg.docs
        else lineage_report is not None and lineage_report.ok
    )
    overall_ok = (
        not errors
        and create_result.ok
        and pre_cutover_gates_ok
        and comp_ok
        and lin_ok
    )

    return OrchestratorResult(
        ok=overall_ok,
        physical_index_name=phys_name,
        alias=alias,
        index_action=index_action,
        fingerprint=fingerprint,
        docs_pushed=docs_pushed,
        pre_cutover_gates_ok=pre_cutover_gates_ok,
        lakehouse_tables=lakehouse_results,
        competency=competency_result,
        lineage_report=lineage_report,
        deployment_rows=dep_rows,
        rollback_result=rollback_result,
        graph_model_id=graph_model_id,
        graph_model_action=graph_model_action,
        errors=errors,
        partial_failures=partial_failures,
    )
