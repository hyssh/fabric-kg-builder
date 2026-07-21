"""CLI inspection for the SPEC-008 canonical semantic contract."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import click

from fabric_kg_builder.semantic import (
    SemanticArtifactValidationError,
    SemanticContractError,
    SemanticCompileError,
    build_agent_semantic_context,
    build_contract_agent_instructions,
    build_graph_projection,
    build_persisted_query_schema,
    compile_semantic_bundle,
    load_semantic_bundle,
    load_semantic_model_artifacts,
    validate_approved_contract,
    validate_compiled_semantic_artifacts,
)
from fabric_kg_builder.serving.graph_model import (
    build_graph_model_parts,
    write_graph_mapping_artifact,
)
from fabric_kg_builder.runtime import (
    CompetencyContractError,
    compile_competency_contract,
    write_competency_contract,
)


def _load_compiled_bundle(
    *,
    contract_path: str,
    mappings_path: str,
    vocabulary_path: str,
    ids_lock_path: str,
    ontology_name: str | None = None,
    data_version: str = "not-observed",
    data_dir: str | None = None,
    quality_report_path: str | None = None,
):
    bundle = load_semantic_bundle(
        contract_path=contract_path,
        mappings_path=mappings_path,
        vocabulary_path=vocabulary_path,
        ids_lock_path=ids_lock_path,
        require_approval=True,
    )
    quality_report = None
    if quality_report_path:
        path = Path(quality_report_path)
        try:
            quality_report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SemanticCompileError(
                f"Could not read semantic quality report '{path}': {exc}"
            ) from exc
    return bundle, compile_semantic_bundle(
        bundle,
        ontology_name=ontology_name,
        data_version=data_version,
        data_dir=data_dir,
        quality_report=quality_report,
    )


def _load_semantic_metadata(semantic_dir: Path) -> tuple[str, str]:
    try:
        contract = json.loads(
            (semantic_dir / "normalized-contract.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise SemanticCompileError(
            f"Could not read normalized semantic contract from "
            f"'{semantic_dir}': {exc}"
        ) from exc
    name = str(contract.get("name") or "").strip()
    description = str(contract.get("description") or "").strip()
    if not name or not description:
        raise SemanticCompileError(
            "Normalized semantic contract must contain name and description."
        )
    return name, description


def _semantic_bundle_options(command):
    command = click.option(
        "--ids-lock",
        "ids_lock_path",
        default="ontology/ids.lock.json",
        show_default=True,
        type=click.Path(dir_okay=False),
        help="Stable semantic and Fabric ID lock.",
    )(command)
    command = click.option(
        "--vocabulary",
        "vocabulary_path",
        default="ontology/vocabulary.yaml",
        show_default=True,
        type=click.Path(dir_okay=False),
        help="Controlled vocabulary YAML.",
    )(command)
    command = click.option(
        "--mappings",
        "mappings_path",
        default="ontology/mappings.yaml",
        show_default=True,
        type=click.Path(dir_okay=False),
        help="Semantic-to-physical mappings YAML.",
    )(command)
    command = click.option(
        "--contract",
        "contract_path",
        default="ontology/contract.yaml",
        show_default=True,
        type=click.Path(dir_okay=False),
        help="Approved canonical semantic contract YAML.",
    )(command)
    return command


@click.command(
    "inspect-ontology",
    context_settings={"max_content_width": 120},
)
@click.option(
    "--contract",
    "contract_path",
    default="ontology/contract.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Canonical semantic contract YAML.",
)
@click.option(
    "--mappings",
    "mappings_path",
    default="ontology/mappings.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Semantic-to-physical mappings YAML.",
)
@click.option(
    "--vocabulary",
    "vocabulary_path",
    default="ontology/vocabulary.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Controlled vocabulary YAML.",
)
@click.option(
    "--ids-lock",
    "ids_lock_path",
    default="ontology/ids.lock.json",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Stable semantic and Fabric ID lock.",
)
@click.option(
    "--format",
    "output_format",
    default="table",
    show_default=True,
    type=click.Choice(["table", "json"]),
    help="Human-readable or machine-readable report.",
)
@click.option(
    "--require-approved/--allow-draft",
    default=False,
    show_default=True,
    help="Fail unless approval metadata matches the normalized contract hash.",
)
def inspect_ontology_cmd(
    contract_path: str,
    mappings_path: str,
    vocabulary_path: str,
    ids_lock_path: str,
    output_format: str,
    require_approved: bool,
) -> None:
    """Inspect semantic meaning, mappings, IDs, and compile readiness."""
    try:
        bundle = load_semantic_bundle(
            contract_path=contract_path,
            mappings_path=mappings_path,
            vocabulary_path=vocabulary_path,
            ids_lock_path=ids_lock_path,
            require_approval=False,
        )
    except SemanticContractError as exc:
        raise click.ClickException(str(exc)) from exc

    findings: list[dict[str, str]] = []
    try:
        validate_approved_contract(bundle.contract)
        ready_for_compile = True
    except SemanticContractError as exc:
        ready_for_compile = False
        findings.append(
            {
                "severity": "error" if require_approved else "warning",
                "code": "SEM_APPROVAL_REQUIRED",
                "message": str(exc),
            }
        )
    if require_approved and not ready_for_compile:
        raise click.ClickException(findings[0]["message"])

    entity_statuses = Counter(
        entity.publication_status for entity in bundle.contract.entity_types
    )
    relationship_statuses = Counter(
        relationship.publication_status
        for relationship in bundle.contract.relationship_types
    )
    core_entities = [
        {
            "id": entity.id,
            "name": entity.name,
            "business_name": entity.business_name,
            "abstract": entity.abstract,
            "parent": entity.parent,
        }
        for entity in bundle.contract.entity_types
        if entity.publication_status == "core"
    ]
    core_relationships = [
        {
            "id": relationship.id,
            "predicate": relationship.predicate,
            "business_name": relationship.business_name,
            "source_type": relationship.source_type,
            "target_type": relationship.target_type,
            "evidence_policy": relationship.evidence_policy,
        }
        for relationship in bundle.contract.relationship_types
        if relationship.publication_status == "core"
    ]
    report = {
        "schema_version": bundle.contract.schema_version,
        "contract_version": bundle.contract.contract_version,
        "contract_name": bundle.contract.name,
        "contract_hash": bundle.contract_hash,
        "approval_status": bundle.contract.approval.status,
        "ready_for_compile": ready_for_compile,
        "entity_counts": dict(sorted(entity_statuses.items())),
        "relationship_counts": dict(sorted(relationship_statuses.items())),
        "entity_mapping_count": len(bundle.mappings.entity_types),
        "relationship_mapping_count": len(bundle.mappings.relationship_types),
        "vocabulary_term_count": len(bundle.vocabulary.terms),
        "core_entity_types": core_entities,
        "core_relationship_types": core_relationships,
        "findings": findings,
    }
    if output_format == "json":
        click.echo(json.dumps(report, indent=2, sort_keys=True))
        return

    click.echo(f"[inspect-ontology] contract       : {report['contract_name']}")
    click.echo(f"[inspect-ontology] version        : {report['contract_version']}")
    click.echo(f"[inspect-ontology] contract hash  : {report['contract_hash']}")
    click.echo(f"[inspect-ontology] approval       : {report['approval_status']}")
    click.echo(f"[inspect-ontology] compile ready  : {report['ready_for_compile']}")
    click.echo(
        "[inspect-ontology] entity types   : "
        f"{sum(entity_statuses.values())} {dict(sorted(entity_statuses.items()))}"
    )
    click.echo(
        "[inspect-ontology] relationships  : "
        f"{sum(relationship_statuses.values())} "
        f"{dict(sorted(relationship_statuses.items()))}"
    )
    for entity in core_entities:
        parent = f" parent={entity['parent']}" if entity["parent"] else ""
        abstract = " abstract" if entity["abstract"] else ""
        click.echo(
            f"[inspect-ontology] entity         : {entity['id']} "
            f"({entity['business_name']}){abstract}{parent}"
        )
    for relationship in core_relationships:
        click.echo(
            f"[inspect-ontology] relationship   : "
            f"{relationship['source_type']} -[{relationship['predicate']}]-> "
            f"{relationship['target_type']} evidence={relationship['evidence_policy']}"
        )
    for finding in findings:
        click.echo(
            f"[inspect-ontology] {finding['severity'].upper()} "
            f"{finding['code']}: {finding['message']}"
        )


@click.command(
    "compile-semantic",
    context_settings={"max_content_width": 120},
)
@_semantic_bundle_options
@click.option(
    "--out",
    "output_path",
    default="build/semantic",
    show_default=True,
    type=click.Path(),
    help="Output directory for shared compiler artifacts.",
)
@click.option(
    "--ontology-name",
    default=None,
    type=str,
    help="Optional Fabric Ontology display name override.",
)
@click.option(
    "--data-version",
    default="not-observed",
    show_default=True,
    help="Opaque canonical data/run version recorded in the sealed manifest.",
)
@click.option(
    "--data-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Optional canonical Parquet directory used only for availability evidence.",
)
@click.option(
    "--quality-report",
    "quality_report_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Passed redacted semantic-quality-report.json from enrichment.",
)
def compile_semantic_cmd(
    contract_path: str,
    mappings_path: str,
    vocabulary_path: str,
    ids_lock_path: str,
    output_path: str,
    ontology_name: str | None,
    data_version: str,
    data_dir: str | None,
    quality_report_path: str | None,
) -> None:
    """Compile one approved contract into Ontology, Graph, and agent inputs."""
    try:
        _bundle, compiled = _load_compiled_bundle(
            contract_path=contract_path,
            mappings_path=mappings_path,
            vocabulary_path=vocabulary_path,
            ids_lock_path=ids_lock_path,
            ontology_name=ontology_name,
            data_version=data_version,
            data_dir=data_dir,
            quality_report_path=quality_report_path,
        )
        out = compiled.write(output_path)
    except (SemanticContractError, SemanticCompileError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"[compile-semantic] contract hash : {compiled.contract_hash}")
    click.echo(
        f"[compile-semantic] entity types  : {len(compiled.graph_entity_types)}"
    )
    click.echo(
        f"[compile-semantic] relationships : {len(compiled.graph_relationships)}"
    )
    click.echo(
        "[compile-semantic] manifest hash : "
        f"{compiled.semantic_model_manifest.manifest_hash}"
    )
    click.echo(f"[compile-semantic] output        : {out.resolve()}")


@click.command(
    "compile-graph",
    context_settings={"max_content_width": 120},
)
@_semantic_bundle_options
@click.option(
    "--out",
    "output_path",
    default="build/graph",
    show_default=True,
    type=click.Path(),
    help="Output directory for Graph definition and label catalog.",
)
@click.option(
    "--semantic-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help=(
        "Sealed compile-semantic output. When supplied, Graph compilation "
        "consumes its manifest, crosswalk, and materialization plan."
    ),
)
@click.option(
    "--workspace-id",
    default="",
    help="Fabric workspace ID; may be empty for offline review.",
)
@click.option(
    "--lakehouse-id",
    default="",
    help="Fabric Lakehouse item ID; may be empty for offline review.",
)
@click.option("--schema", default="dbo", show_default=True)
@click.option(
    "--model-name",
    default="kg_graph_model",
    show_default=True,
    help="Graph Model display name.",
)
def compile_graph_cmd(
    contract_path: str,
    mappings_path: str,
    vocabulary_path: str,
    ids_lock_path: str,
    output_path: str,
    semantic_dir: str | None,
    workspace_id: str,
    lakehouse_id: str,
    schema: str,
    model_name: str,
) -> None:
    """Compile Graph definition parts from exact contract-owned labels."""
    try:
        if semantic_dir:
            loaded = load_semantic_model_artifacts(semantic_dir)
            manifest = loaded.manifest
            materialization_plan = loaded.materialization_plan
            (
                graph_entity_types,
                graph_node_labels,
                graph_relationships,
                label_catalog,
            ) = build_graph_projection(
                manifest,
                materialization_plan,
            )
            contract_hash = manifest.semantic_contract_hash
            semantic_crosswalk = loaded.crosswalk
        else:
            _bundle, compiled = _load_compiled_bundle(
                contract_path=contract_path,
                mappings_path=mappings_path,
                vocabulary_path=vocabulary_path,
                ids_lock_path=ids_lock_path,
            )
            manifest = compiled.semantic_model_manifest
            materialization_plan = compiled.materialization_plan
            graph_entity_types = compiled.graph_entity_types
            graph_node_labels = compiled.graph_node_labels
            graph_relationships = compiled.graph_relationships
            label_catalog = compiled.label_catalog
            contract_hash = compiled.contract_hash
            semantic_crosswalk = compiled.semantic_crosswalk
        entity_table_by_id = {
            table.semantic_id: table
            for table in materialization_plan.entity_tables
        }
        node_table_bindings = {
            entity.canonical_name: {
                "table": entity.physical_source_table,
                "node_type_alias": entity.graph_projection.alias,
                "entity_id_column": entity_table_by_id[
                    entity.semantic_id
                ].entity_id_column,
                "property_columns": [
                    column.column_name
                    for column in entity_table_by_id[
                        entity.semantic_id
                    ].columns
                ],
            }
            for entity in manifest.entity_types
        }
        parts = build_graph_model_parts(
            entity_types=list(graph_entity_types),
            relationship_pairs=list(graph_relationships),
            node_labels=graph_node_labels,
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_id,
            schema=schema,
            model_name=model_name,
            node_table_bindings=node_table_bindings,
        )
        out = Path(output_path)
        out.mkdir(parents=True, exist_ok=True)
        write_graph_mapping_artifact(
            out / "graph-definition.json",
            parts,
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_id,
            schema=schema,
            model_name=model_name,
        )
        (out / "label-catalog.json").write_text(
            json.dumps(label_catalog, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        graph_artifacts = {}
        for path in (out / "graph-definition.json", out / "label-catalog.json"):
            graph_artifacts[path.name] = (
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            )
        graph_artifact_set_hash = hashlib.sha256(
            json.dumps(
                graph_artifacts,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        (out / "graph-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "contract_hash": contract_hash,
                    "semantic_model_manifest_hash": manifest.manifest_hash,
                    "semantic_crosswalk_hash": (
                        "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                semantic_crosswalk.model_dump(mode="json"),
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                    ),
                    "materialization_plan_hash": (
                        "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                materialization_plan.model_dump(mode="json"),
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                    ),
                    "artifact_set_hash": f"sha256:{graph_artifact_set_hash}",
                    "artifacts": graph_artifacts,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except (SemanticContractError, SemanticCompileError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"[compile-graph] contract hash : {contract_hash}")
    click.echo(f"[compile-graph] node labels   : {len(graph_entity_types)}")
    click.echo(
        f"[compile-graph] edge labels   : {len(graph_relationships)}"
    )
    click.echo(f"[compile-graph] output        : {out.resolve()}")


_COMPILE_AGENT_EPILOG = """\b
This command creates the shared grounding consumed by deploy-data-agent and the
Foundry prompt agent. It derives exact source elements and descriptions from the
sealed semantic manifest; competency questions and Graph probes provide
validated examples.

Authoring template (replace every {{...}} token before compilation):
\b
  Domain instruction:
    Answer questions about {{DOMAIN_NAME}} using only persisted evidence.
    Use Search for document facts and Graph for relationship/path questions.
    Cite evidence IDs and say when the persisted sources do not support an answer.

  Entity description:
    {{ENTITY_NAME}}: {{ENTITY_DESCRIPTION}}.
    Allowed properties: {{PROPERTY_NAME_1}}, {{PROPERTY_NAME_2}}.

  Relationship description:
    Source entity: {{SOURCE_ENTITY}}
    Relationship: {{RELATIONSHIP_NAME}}
    Target entity: {{TARGET_ENTITY}}
    {{RELATIONSHIP_DESCRIPTION}}.

  Competency example:
    Question: Which {{TARGET_ENTITY}} is related to {{SOURCE_ENTITY}}?
    GQL:
      MATCH (s:{{SOURCE_ENTITY}})
            -[r:{{RELATIONSHIP_NAME}}]->
            (t:{{TARGET_ENTITY}})
         RETURN s.{{DISPLAY_PROPERTY}}, t.{{DISPLAY_PROPERTY}}, r.evidence_id LIMIT 100

Rules for users, Copilot, and AI agents:
  - Replace placeholders with exact case-sensitive semantic/Graph names.
  - Use only properties owned by the selected entity type.
  - Preserve relationship direction; never reverse or invent an edge.
  - Return scalar IDs/display properties and evidence_id, not whole nodes/edges.
  - Keep examples bounded (LIMIT 100) and validate them in the competency suite.

PowerShell example:
\b
  fabric-kg compile-agent --semantic-dir build\\semantic --out build\\agents --domain-context "Building operations and maintenance" --competency-suite evaluation\\competency.yaml
"""


@click.command(
    "compile-agent",
    epilog=_COMPILE_AGENT_EPILOG,
    context_settings={"max_content_width": 120},
)
@_semantic_bundle_options
@click.option(
    "--out",
    "output_path",
    default="build/agents",
    show_default=True,
    type=click.Path(),
    help="Output directory for contract-owned agent grounding.",
)
@click.option(
    "--semantic-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help=(
        "Sealed compile-semantic output. When supplied, agent schema "
        "compilation consumes its manifest and canonical crosswalk."
    ),
)
@click.option(
    "--question",
    "questions",
    multiple=True,
    help="Competency question; repeat for multiple questions.",
)
@click.option(
    "--domain-context",
    default="",
    help="Approved domain context to include in the grounding artifact.",
)
@click.option(
    "--competency-suite",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "Route-aware competency YAML/JSON. Expected semantic IDs and Graph "
        "directions are validated against the approved contract."
    ),
)
def compile_agent_cmd(
    contract_path: str,
    mappings_path: str,
    vocabulary_path: str,
    ids_lock_path: str,
    output_path: str,
    semantic_dir: str | None,
    questions: tuple[str, ...],
    domain_context: str,
    competency_suite: str | None,
) -> None:
    """Compile instructions, source semantics, and validated agent examples."""
    try:
        if semantic_dir:
            semantic_root = Path(semantic_dir)
            loaded = load_semantic_model_artifacts(semantic_root)
            contract_name, contract_description = _load_semantic_metadata(
                semantic_root
            )
            semantic_context = build_agent_semantic_context(
                loaded.manifest,
                loaded.crosswalk,
                contract_name=contract_name,
                contract_description=contract_description,
            )
            contract_hash = loaded.manifest.semantic_contract_hash
            semantic_model_manifest_hash = loaded.manifest.manifest_hash
            semantic_manifest = loaded.manifest
            semantic_crosswalk = loaded.crosswalk
            semantic_crosswalk_hash = semantic_context[
                "semantic_crosswalk_hash"
            ]
        else:
            _bundle, compiled = _load_compiled_bundle(
                contract_path=contract_path,
                mappings_path=mappings_path,
                vocabulary_path=vocabulary_path,
                ids_lock_path=ids_lock_path,
            )
            semantic_context = compiled.agent_semantic_context
            contract_hash = compiled.contract_hash
            semantic_model_manifest_hash = (
                compiled.semantic_model_manifest.manifest_hash
            )
            semantic_manifest = compiled.semantic_model_manifest
            semantic_crosswalk = compiled.semantic_crosswalk
            semantic_crosswalk_hash = semantic_context[
                "semantic_crosswalk_hash"
            ]
        instructions = build_contract_agent_instructions(
            semantic_context,
            competency_questions=questions,
            domain_context=domain_context,
        )
        out = Path(output_path)
        out.mkdir(parents=True, exist_ok=True)
        instructions_path = out / "instructions.md"
        context_path = out / "semantic-context.json"
        instructions_path.write_text(instructions, encoding="utf-8")
        context_path.write_text(
            json.dumps(
                semantic_context,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        query_schema = build_persisted_query_schema(
            semantic_manifest,
            semantic_crosswalk,
        )
        query_schema_path = out / "persisted-query-schema.json"
        query_schema_path.write_text(
            json.dumps(
                query_schema.model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        competency_path = out / "competency-contract.json"
        competency_hash = None
        competency_count = 0
        competency_status = "not_configured"
        if competency_suite:
            competency_contract = compile_competency_contract(
                competency_suite,
                contract_hash=contract_hash,
                semantic_context=semantic_context,
                query_schema=query_schema,
            )
            write_competency_contract(competency_contract, competency_path)
            competency_hash = (
                "sha256:"
                + hashlib.sha256(competency_path.read_bytes()).hexdigest()
            )
            competency_count = len(competency_contract.cases)
            competency_status = "compiled"
        elif questions:
            competency_status = "requires_route_authoring"
        instruction_hash = hashlib.sha256(
            instructions_path.read_bytes()
        ).hexdigest()
        context_hash = hashlib.sha256(context_path.read_bytes()).hexdigest()
        (out / "agent-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "contract_hash": contract_hash,
                    "semantic_model_manifest_hash": (
                        semantic_model_manifest_hash
                    ),
                    "semantic_crosswalk_hash": semantic_crosswalk_hash,
                    "persisted_query_schema_hash": (
                        query_schema.schema_hash
                    ),
                    "instruction_hash": f"sha256:{instruction_hash}",
                    "semantic_context_hash": f"sha256:{context_hash}",
                    "competency_question_count": len(questions),
                    "competency_case_count": competency_count,
                    "competency_contract_hash": competency_hash,
                    "competency_status": competency_status,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except (
        CompetencyContractError,
        SemanticContractError,
        SemanticCompileError,
        ValueError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"[compile-agent] contract hash : {contract_hash}")
    click.echo(f"[compile-agent] instruction hash : sha256:{instruction_hash}")
    click.echo(f"[compile-agent] competency status : {competency_status}")
    click.echo(f"[compile-agent] output        : {out.resolve()}")


@click.command(
    "validate-artifacts",
    context_settings={"max_content_width": 120},
)
@click.option(
    "--build-dir",
    default="build",
    show_default=True,
    type=click.Path(),
    help="Build directory containing semantic, Ontology, Graph, agent, and Search outputs.",
)
@click.option(
    "--require-search/--no-require-search",
    default=True,
    show_default=True,
    help="Require and validate the AI Search compiler surface.",
)
@click.option(
    "--require-competency/--no-require-competency",
    default=False,
    show_default=True,
    help="Require a compiled route-aware competency contract.",
)
@click.option(
    "--require-model-authority/--allow-legacy-model-authority",
    default=True,
    show_default=True,
    help="Require the sealed SPEC-008A manifest/crosswalk/materialization set.",
)
def validate_artifacts_cmd(
    build_dir: str,
    require_search: bool,
    require_competency: bool,
    require_model_authority: bool,
) -> None:
    """Validate cross-artifact semantic IDs, labels, hashes, and policies."""
    try:
        report = validate_compiled_semantic_artifacts(
            build_dir,
            require_search=require_search,
            require_competency=require_competency,
            require_model_authority=require_model_authority,
        )
    except SemanticArtifactValidationError as exc:
        for finding in exc.findings:
            click.echo(
                f"[validate-artifacts] {finding.code}: {finding.message}",
                err=True,
            )
        raise click.ClickException(
            f"{len(exc.findings)} semantic artifact validation failure(s)."
        ) from exc
    click.echo(
        "[validate-artifacts] PASS "
        f"contract={report['contract_hash']} "
        f"entities={report['entity_type_count']} "
        f"relationships={report['relationship_type_count']}"
    )
