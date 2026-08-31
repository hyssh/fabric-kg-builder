"""Resumable end-to-end build and deployment orchestration."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import click
import yaml
from click.testing import CliRunner


_BUILD_DEPLOY_EPILOG = """\b
Examples:
  fabric-kg build-deploy --input .\\assets --domain-contract .\\domain.yaml --env dev
  fabric-kg build-deploy --input .\\assets --domain-contract .\\domain.yaml --env dev --dry-run
  fabric-kg build-deploy --input .\\assets --domain-contract .\\domain.yaml --env dev \
    --deploy-knowledge --deploy-agent --deploy-app --graph-preview-acknowledged

\b
Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""

_STATE_SCHEMA = "fabric-kg-build-deploy/1.0"
_LEDGER_SCHEMA = "fabric-kg-resource-ledger/1.0"


class BuildDeployError(click.ClickException):
    """Raised when a pipeline stage cannot complete truthfully."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


class _RunState:
    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        environment: str,
        resume: bool,
    ) -> None:
        self.path = path
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("run_id") != run_id:
                raise BuildDeployError(
                    f"State at {path} belongs to run "
                    f"{payload.get('run_id')!r}, not {run_id!r}."
                )
            if payload.get("environment") != environment:
                raise BuildDeployError(
                    f"State at {path} targets environment "
                    f"{payload.get('environment')!r}, not {environment!r}."
                )
            self.data = payload
        else:
            if resume:
                raise BuildDeployError(
                    f"--resume requested but no state exists at {path}."
                )
            self.data = {
                "schema": _STATE_SCHEMA,
                "run_id": run_id,
                "environment": environment,
                "status": "running",
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "stages": {},
            }
            self.save()

    def save(self) -> None:
        self.data["updated_at"] = _utc_now()
        _atomic_json(self.path, self.data)

    def execute(
        self,
        name: str,
        action: Callable[[], dict[str, Any] | None],
        *,
        resume: bool,
    ) -> dict[str, Any]:
        current = self.data.setdefault("stages", {}).get(name, {})
        if resume and current.get("status") == "succeeded":
            click.echo(f"[build-deploy] SKIP {name} (already succeeded)")
            return dict(current.get("details") or {})

        stage = {
            "status": "running",
            "started_at": _utc_now(),
            "completed_at": None,
            "details": {},
        }
        self.data["stages"][name] = stage
        self.data["status"] = "running"
        self.save()
        click.echo(f"\n[build-deploy] START {name}")
        try:
            details = action() or {}
        except Exception as exc:
            stage["status"] = "failed"
            stage["completed_at"] = _utc_now()
            stage["error"] = str(exc)
            self.data["status"] = "failed"
            self.save()
            raise
        stage["status"] = "succeeded"
        stage["completed_at"] = _utc_now()
        stage["details"] = details
        self.save()
        click.echo(f"[build-deploy] DONE  {name}")
        return details

    def complete(self, *, dry_run: bool = False) -> None:
        self.data["status"] = "planned" if dry_run else "succeeded"
        self.data["completed_at"] = _utc_now()
        self.save()


def _invoke_cli(
    args: list[str],
    *,
    config_path: Path,
    environment: str,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    from fabric_kg_builder.cli.main import cli

    command = [
        "--config",
        str(config_path),
        "--env",
        environment,
        *args,
    ]
    result = CliRunner().invoke(
        cli,
        command,
        env=extra_env,
        catch_exceptions=True,
    )
    if result.output:
        click.echo(result.output.rstrip())
    if result.exit_code != 0:
        detail = str(result.exception) if result.exception else "unknown error"
        raise BuildDeployError(
            f"Command failed ({result.exit_code}): fabric-kg "
            f"{' '.join(args)}\n{detail}"
        )
    return {"command": ["fabric-kg", *args], "exit_code": result.exit_code}


def _runtime_paths(run_root: Path) -> dict[str, Path]:
    build = run_root / "build"
    return {
        "build": build,
        "inspection": build / "inspection",
        "enriched": build / "enriched",
        "enriched_dense": build / "enriched_dense",
        "parquet": build / "parquet",
        "semantic": build / "semantic",
        "ontology": build / "ontology",
        "search": build / "search",
        "graph": build / "graph",
        "agents": build / "agents",
        "dist": run_root / "dist",
        "registry": run_root / "registry.json",
        "metadata": run_root / ".foundry" / "agent-metadata.yaml",
        "release": run_root / "release",
    }


def _runtime_environment(
    *,
    outputs: dict[str, Any],
    run_root: Path,
    environment: str,
) -> dict[str, str]:
    """Map imported infrastructure outputs to child-command runtime settings."""
    candidates = {
        "FABRIC_KG_INFRA_OUTPUTS_PATH": (
            run_root / "infra" / environment / "outputs.json"
        ),
        "AZURE_AI_FOUNDRY_ENDPOINT": (
            outputs.get("foundryProjectEndpoint")
            or outputs.get("foundryEndpoint")
        ),
        "AZURE_AI_PROJECT_ENDPOINT": outputs.get("foundryProjectEndpoint"),
        "AZURE_OPENAI_ENDPOINT": outputs.get("foundryOpenAIEndpoint"),
        "AZURE_AI_CHAT_DEPLOYMENT": outputs.get("chatDeploymentName"),
        "AZURE_AI_EMBEDDING_DEPLOYMENT": outputs.get(
            "embeddingDeploymentName"
        ),
        "AZURE_SEARCH_ENDPOINT": outputs.get("searchEndpoint"),
        "AZURE_STORAGE_ACCOUNT": outputs.get("storageAccountName"),
        "AZURE_STORAGE_ACCOUNT_URL": outputs.get("blobEndpoint"),
        "AZURE_BLOB_CONTAINER": outputs.get("containerName"),
        "AZURE_DOCINTEL_ENDPOINT": outputs.get(
            "documentIntelligenceEndpoint"
        ),
        "FABRIC_WORKSPACE_ID": outputs.get("fabricWorkspaceId"),
        "FABRIC_LAKEHOUSE_ID": outputs.get("fabricLakehouseId"),
        "FABRIC_ONTOLOGY_ID": outputs.get("fabricOntologyId"),
        "FABRIC_GRAPH_MODEL_ID": outputs.get("fabricGraphModelId"),
        "FABRIC_KG_FABRIC_WORKSPACE_ID": outputs.get("fabricWorkspaceId"),
        "FABRIC_KG_GRAPH_MODEL_ID": outputs.get("fabricGraphModelId"),
        "FABRIC_KG_BLOB_ACCOUNT_URL": outputs.get("blobEndpoint"),
        "FABRIC_KG_BLOB_CONTAINER": outputs.get("containerName"),
    }
    return {
        name: str(value)
        for name, value in candidates.items()
        if value is not None and str(value).strip()
    }


def _semantic_compatibility_gate(
    *,
    current_contract_path: Path,
    baseline_contract_path: Path,
    report_path: Path,
    approve_breaking_migration: bool,
    initialize_baseline: bool,
    enforce: bool,
) -> dict[str, Any]:
    """Classify semantic changes and fail closed on unapproved breakage."""
    from fabric_kg_builder.semantic.compatibility import (
        CompatibilityLevel,
        classify_contract_change,
    )
    from fabric_kg_builder.semantic.service import load_semantic_contract

    current = load_semantic_contract(current_contract_path)
    baseline_exists = baseline_contract_path.exists()
    if baseline_exists:
        previous = load_semantic_contract(baseline_contract_path)
        report = classify_contract_change(previous, current)
        payload = report.model_dump(mode="json")
    else:
        payload = {
            "level": "baseline" if initialize_baseline else "baseline_missing",
            "previous_version": None,
            "current_version": current.contract_version,
            "changes": [],
        }
    payload["baseline_contract_path"] = str(baseline_contract_path)
    payload["baseline_initialization_approved"] = (
        initialize_baseline and not baseline_exists
    )
    payload["breaking_migration_approved"] = (
        approve_breaking_migration
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if (
        enforce
        and payload["level"] == "baseline_missing"
    ):
        raise BuildDeployError(
            "No prior semantic contract baseline is available. Supply "
            "--previous-semantic-contract for an existing deployment or "
            "--initialize-semantic-baseline for a confirmed new target."
        )
    if (
        enforce
        and payload["level"] == CompatibilityLevel.BREAKING.value
        and not approve_breaking_migration
    ):
        raise BuildDeployError(
            "Breaking semantic contract changes require "
            "--approve-breaking-semantic-migration before live Ontology or "
            "Graph mutation."
        )
    return payload


def _record_semantic_baseline(
    source: Path,
    destination: Path,
) -> dict[str, Any]:
    """Persist the last successfully mutated semantic contract locally."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_bytes(source.read_bytes())
    temporary.replace(destination)
    return {"semantic_baseline": str(destination)}


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _knowledge_search_index_name(
    serving_receipt_path: Path,
    fallback_index_name: str,
) -> str:
    """Use the physical Search index because knowledge sources reject aliases."""
    receipt = _read_json_object(serving_receipt_path)
    return str(receipt.get("physical_index_name") or fallback_index_name)


def _write_semantic_deployment_receipt(
    *,
    run_id: str,
    environment: str,
    paths: dict[str, Path],
    state: _RunState,
) -> dict[str, Any]:
    semantic = _read_json_object(
        paths["semantic"] / "semantic-manifest.json"
    )
    ontology = _read_json_object(
        paths["ontology"] / "ontology-manifest.json"
    )
    graph = _read_json_object(paths["graph"] / "graph-manifest.json")
    search = _read_json_object(paths["search"] / "search-manifest.json")
    agent = _read_json_object(paths["agents"] / "agent-manifest.json")
    package = _read_json_object(
        paths["dist"] / "fabric-kg-package" / "manifest.json"
    )
    ontology_deployment = _read_json_object(
        paths["release"] / "ontology-deployment.json"
    )
    serving_deployment = _read_json_object(
        paths["release"] / "serving-deployment.json"
    )
    persisted_projection_path = (
        paths["release"] / "persisted-projection-receipt.json"
    )
    persisted_projection = _read_json_object(persisted_projection_path)
    agent_publication_path = (
        paths["release"] / "agent-publication-receipt.json"
    )
    agent_publication = _read_json_object(agent_publication_path)
    knowledge = (
        state.data.get("stages", {})
        .get("deploy_knowledge", {})
        .get("details", {})
    )
    receipt = {
        "schema": "fabric-kg.semantic-deployment-receipt.v1",
        "run_id": run_id,
        "environment": environment,
        "created_at_utc": _utc_now(),
        "semantic_contract_hash": semantic.get("contract_hash"),
        "semantic_artifact_set_hash": semantic.get("artifact_set_hash"),
        "ontology_artifact_set_hash": ontology.get("artifact_set_hash"),
        "graph_artifact_set_hash": graph.get("artifact_set_hash"),
        "search_artifact_set_hash": search.get("artifact_set_hash"),
        "instruction_hash": (
            agent_publication.get("compiled_instruction_hash")
            or agent_publication.get("package_instruction_hash")
            or (
                knowledge.get("compiled_instruction_hash")
                if isinstance(knowledge, dict)
                else None
            )
            or agent.get("instruction_hash")
        ),
        "deployed_instruction_hash": (
            agent_publication.get("published_instruction_hash")
            or (
                knowledge.get("deployed_instruction_hash")
                if isinstance(knowledge, dict)
                else None
            )
        ),
        "semantic_context_hash": agent.get("semantic_context_hash"),
        "persisted_query_schema_hash": agent.get(
            "persisted_query_schema_hash"
        ),
        "competency_contract_hash": agent.get(
            "competency_contract_hash"
        ),
        "package_hash": package.get("package_hash"),
        "ontology_item_id": ontology_deployment.get("ontology_item_id"),
        "graph_model_id": serving_deployment.get("graph_model_id"),
        "semantic_model_manifest_hash": persisted_projection.get(
            "semantic_model_manifest_hash"
        ),
        "ontology_persisted_projection_hash": persisted_projection.get(
            "ontology_persisted_projection_hash"
        ),
        "graph_persisted_projection_hash": persisted_projection.get(
            "graph_persisted_projection_hash"
        ),
        "persisted_projection_receipt_hash": (
            "sha256:"
            + hashlib.sha256(
                persisted_projection_path.read_bytes()
            ).hexdigest()
            if persisted_projection_path.is_file()
            else None
        ),
        "search_index_name": serving_deployment.get(
            "physical_index_name"
        ),
        "data_agent_id": (
            agent_publication.get("data_agent_item_id")
            or (
                knowledge.get("data_agent_id")
                if isinstance(knowledge, dict)
                else None
            )
        ),
        "data_agent_published": (
            agent_publication.get("publication_status") == "published"
            or (
                knowledge.get("data_agent_published") is True
                if isinstance(knowledge, dict)
                else False
            )
        ),
        "data_agent_target_mode": agent_publication.get("target_mode"),
        "data_agent_actions": agent_publication.get("actions"),
        "data_agent_workspace_name": agent_publication.get(
            "workspace_name"
        ),
        "data_agent_workspace_id": agent_publication.get("workspace_id"),
        "source_selection_hash": agent_publication.get(
            "published_source_selection_hash"
        ),
        "selected_element_hash": agent_publication.get(
            "published_selected_element_hash"
        ),
        "agent_schema_sidecar_hash": agent_publication.get(
            "agent_schema_sidecar_hash"
        ),
        "agent_publication_receipt_hash": (
            "sha256:"
            + hashlib.sha256(agent_publication_path.read_bytes()).hexdigest()
            if agent_publication_path.is_file()
            else None
        ),
        "knowledge_base_id": (
            knowledge.get("knowledge_base_id")
            if isinstance(knowledge, dict)
            else None
        ),
        "stage_statuses": {
            name: stage.get("status")
            for name, stage in sorted(
                state.data.get("stages", {}).items()
            )
            if isinstance(stage, dict)
        },
    }
    hashes = [
        semantic.get("contract_hash"),
        ontology.get("contract_hash"),
        graph.get("contract_hash"),
        agent.get("contract_hash"),
        search.get("contract_hash") if search else None,
        package.get("contract_hash"),
    ]
    populated_hashes = {value for value in hashes if value}
    receipt["contract_hash_consistent"] = len(populated_hashes) == 1
    target = paths["release"] / "deployment-receipt.json"
    _atomic_json(target, receipt)
    return {
        "path": str(target),
        "semantic_contract_hash": receipt["semantic_contract_hash"],
        "contract_hash_consistent": receipt["contract_hash_consistent"],
    }


def _import_infrastructure_state(
    *,
    environment: str,
    run_root: Path,
    explicit_outputs_path: Path | None,
) -> dict[str, Any]:
    """Copy existing non-secret infrastructure state into the run boundary."""
    target_dir = run_root / "infra" / environment
    target_outputs = target_dir / "outputs.json"
    candidates = [
        explicit_outputs_path,
        target_outputs,
        Path("build") / "infra" / environment / "outputs.json",
    ]
    source_outputs = next(
        (
            candidate.resolve()
            for candidate in candidates
            if candidate is not None and candidate.exists()
        ),
        None,
    )
    if source_outputs is None:
        checked = ", ".join(
            str(candidate)
            for candidate in candidates
            if candidate is not None
        )
        raise BuildDeployError(
            "--no-provision requires existing non-secret infrastructure "
            f"outputs. Checked: {checked}. Run 'fabric-kg infra apply' first "
            "or pass --infra-outputs."
        )

    try:
        outputs = json.loads(source_outputs.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildDeployError(
            f"Could not load infrastructure outputs from {source_outputs}: {exc}"
        ) from exc
    if not isinstance(outputs, dict) or not outputs:
        raise BuildDeployError(
            f"Infrastructure outputs at {source_outputs} are empty or invalid."
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(target_outputs, outputs)
    source_state = source_outputs.with_name("state.json")
    target_state = target_dir / "state.json"
    if source_state.exists() and source_state.resolve() != target_state.resolve():
        try:
            state_payload = json.loads(source_state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BuildDeployError(
                f"Could not load infrastructure state from {source_state}: {exc}"
            ) from exc
        _atomic_json(target_state, state_payload)

    return {
        "source_outputs": str(source_outputs),
        "run_outputs": str(target_outputs),
        "output_count": len(outputs),
        "state_imported": source_state.exists(),
    }


def _prepare_metadata(source: Path, target: Path, run_token: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.exists() and not target.exists():
        shutil.copy2(source, target)
    payload: dict[str, Any] = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            payload = loaded
    payload.setdefault("schemaVersion", "1.0")
    payload["agentName"] = f"fkg-{run_token}-agent"
    payload.setdefault("defaultEnvironment", "dev")
    payload.pop("deploymentContext", None)
    target.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def _read_environment_config(environment: str) -> dict[str, Any]:
    path = Path("ontology") / "environments" / f"{environment}.json"
    if not path.exists():
        raise BuildDeployError(f"Runtime environment configuration is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise BuildDeployError(f"Runtime environment configuration is invalid: {path}")
    return payload


def _persist_data_agent_identity(
    environment: str,
    *,
    item_id: str,
    display_name: str,
) -> None:
    """Persist the exact Data Agent target for compatible reapplication."""
    path = Path("ontology") / "environments" / f"{environment}.json"
    payload = _read_environment_config(environment)
    fabric = payload.setdefault("fabric", {})
    fabric["data_agent_item_id"] = item_id
    fabric["data_agent_display_name"] = display_name
    _atomic_json(path, payload)


def _compile_dynamic_ontology(
    *,
    parquet_dir: Path,
    output_dir: Path,
    environment: str,
    domain_name: str,
) -> dict[str, Any]:
    from fabric_kg_builder.ontology.fabric_def import (
        build_multitype_ontology_parts,
    )
    from fabric_kg_builder.ontology.multitype_plan import build_plan

    config = _read_environment_config(environment)
    fabric = config.get("fabric", {})
    workspace_id = str(fabric.get("workspace_id", ""))
    lakehouse_id = str(fabric.get("lakehouse_item_id", ""))
    schema = str(fabric.get("schema_name") or "dbo")
    if not workspace_id or not lakehouse_id:
        raise BuildDeployError(
            "Dynamic ontology compilation requires configured Fabric workspace "
            "and Lakehouse IDs."
        )

    plan = build_plan(
        parquet_dir,
        min_type_count=1,
        min_pair_count=1,
        max_pairs=200,
    )
    if not plan.entity_types:
        raise BuildDeployError(
            "No observed entity types were available for ontology compilation."
        )
    parts = build_multitype_ontology_parts(
        workspace_id=workspace_id,
        lakehouse_item_id=lakehouse_id,
        entity_types=[asdict(item) for item in plan.entity_types],
        relationship_pairs=[asdict(item) for item in plan.relationship_pairs],
        schema=schema,
        ontology_name=domain_name,
    )

    if output_dir.exists():
        shutil.rmtree(output_dir)
    encoded_parts: list[dict[str, str]] = []
    for part in parts:
        target = output_dir / part["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(
            part["payload_json"],
            indent=2,
            ensure_ascii=False,
        )
        target.write_text(raw + "\n", encoding="utf-8")
        encoded_parts.append(
            {
                "path": part["path"],
                "payload": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
                "payloadType": "InlineBase64",
            }
        )
    _atomic_json(output_dir / "definition.json", {"parts": encoded_parts})
    _atomic_json(
        output_dir / "multitype-plan.json",
        {
            "entity_types": [asdict(item) for item in plan.entity_types],
            "relationship_pairs": [
                asdict(item) for item in plan.relationship_pairs
            ],
        },
    )
    return {
        "entity_type_count": len(plan.entity_types),
        "relationship_pair_count": len(plan.relationship_pairs),
        "definition": str(output_dir / "definition.json"),
    }


def _ensure_fabric_runtime_access(
    *,
    workspace_id: str,
    principal_id: str,
) -> dict[str, Any]:
    from fabric_kg_builder.infra.fabric_client import (
        DefaultAzureCredentialFabricTransport,
    )

    if not workspace_id or not principal_id:
        raise BuildDeployError(
            "Fabric runtime access requires workspace and managed-identity "
            "principal IDs."
        )
    response = DefaultAzureCredentialFabricTransport().request(
        "POST",
        (
            "https://api.fabric.microsoft.com/v1/workspaces/"
            f"{workspace_id}/roleAssignments"
        ),
        json_body={
            "principal": {
                "id": principal_id,
                "type": "ServicePrincipal",
            },
            "role": "Viewer",
        },
    )
    if response.status_code not in (200, 201, 409):
        raise BuildDeployError(
            "Fabric workspace role assignment failed with HTTP "
            f"{response.status_code}: {response.body[:500]}"
        )
    return {
        "workspace_id": workspace_id,
        "principal_id": principal_id,
        "role": "Viewer",
        "status_code": response.status_code,
    }


def _domain_instruction(domain_contract: Any) -> str:
    return (
        f"Answer only from evidence for the {domain_contract.domain.name} domain. "
        f"The business problem is: {domain_contract.problem.statement} "
        "Use the Graph source for hierarchy, dependencies, and relationships. "
        "Return source-backed results and disclose when evidence is insufficient."
    )


def _update_grounding_metadata(
    *,
    metadata_path: Path,
    environment: str,
    search_connection_id: str,
    search_index_name: str,
    data_agent_connection_id: str,
    knowledge_base_name: str,
) -> None:
    payload = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    env_cfg = payload.setdefault("environments", {}).setdefault(environment, {})
    connections = env_cfg.setdefault("connections", {})
    if search_connection_id:
        connections["search"] = search_connection_id
    connections["fabricDataAgent"] = data_agent_connection_id
    connections.pop("knowledgeBase", None)
    knowledge = env_cfg.setdefault("knowledge", {})
    knowledge["searchIndexName"] = search_index_name
    knowledge["knowledgeBaseName"] = knowledge_base_name
    knowledge.pop("knowledgeBaseMcpEndpoint", None)
    knowledge.setdefault("queryType", "semantic")
    knowledge.setdefault("topK", 5)
    metadata_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def _deploy_knowledge(
    *,
    environment: str,
    run_token: str,
    domain_contract: Any,
    metadata_path: Path,
    outputs: dict[str, Any],
    manifest: Any,
    search_index_name: str,
    semantic_dir: Path,
    persisted_projection_path: Path,
    semantic_context_path: Path,
    agent_instructions_path: Path,
    agent_publication_receipt_path: Path,
    workspace_name: str,
    data_agent_mode: str,
    data_agent_item_id: str | None,
    data_agent_display_name: str | None,
    approve_data_agent_replace: bool,
    require_foundry_search_connection: bool = False,
    deploy_manifest_path: str | None = None,
) -> dict[str, Any]:
    from azure.identity import DefaultAzureCredential

    from fabric_kg_builder.agent.project_connections import (
        FoundryProjectConnectionClient,
    )
    from fabric_kg_builder.knowledge.agent_validation import (
        AgentPublicationError,
        build_public_graph_source_projection,
        build_public_ontology_source_projection,
        build_persisted_agent_grounding,
        deploy_and_validate_data_agent,
    )
    from fabric_kg_builder.knowledge.data_agent import (
        compare_graph_few_shot_semantics,
        DataAgentDefinitionError,
        DataAgentSpec,
        DataSourceSpec,
        DataAgentTargetError,
        FabricDataAgentClient,
        validate_graph_few_shot_examples,
        stage_snapshot_from_spec,
    )
    from fabric_kg_builder.knowledge.models import (
        CapabilityResult,
        _GA_FEATURES,
        _GA_VERSION,
    )
    from fabric_kg_builder.knowledge.search_kb import (
        KnowledgeBaseSpec,
        SearchIndexKnowledgeSourceSpec,
        SearchKbClient,
    )
    from fabric_kg_builder.knowledge.transport import RequestsTransport
    from fabric_kg_builder.semantic import (
        PersistedProjectionReceipt,
        build_contract_agent_instructions,
        build_graph_source_description,
        build_graph_source_instructions,
        build_ontology_source_description,
        build_ontology_source_instructions,
        load_semantic_model_artifacts,
    )
    from fabric_kg_builder.serving.graph_model import GraphModelGQLClient
    # Import early so the except clause below can reference the type (#13 blocker fix).
    from fabric_kg_builder.knowledge.validation import (  # noqa: PLC0415
        DataAgentExampleValidationFailed,
        DataAgentRequiredExampleEmpty,
    )
    config = _read_environment_config(environment)
    fabric = config.get("fabric", {})
    search = config.get("ai_search", {})
    workspace_id = str(fabric.get("workspace_id", ""))
    graph_model_id = str(fabric.get("graph_model_item_id", ""))
    ontology_item_id = str(fabric.get("ontology_item_id", ""))
    lakehouse_item_id = str(fabric.get("lakehouse_item_id", ""))
    search_endpoint = str(search.get("endpoint", "")).rstrip("/")
    if not all((
        workspace_id,
        graph_model_id,
        ontology_item_id,
        lakehouse_item_id,
        search_endpoint,
    )):
        raise BuildDeployError(
            "Knowledge deployment requires Search endpoint, Fabric workspace, "
            "Lakehouse ID, Graph Model ID, and Ontology ID."
        )

    config_item_id = str(fabric.get("data_agent_item_id") or "").strip()
    cli_item_id = str(data_agent_item_id or "").strip()
    if cli_item_id and config_item_id and cli_item_id != config_item_id:
        raise BuildDeployError(
            "--data-agent-id differs from fabric.data_agent_item_id; "
            "refusing an ambiguous target."
        )
    configured_item_id = cli_item_id or config_item_id
    configured_name = str(
        data_agent_display_name
        or fabric.get("data_agent_display_name")
        or f"fkg-{environment}-data-agent"
    ).strip()
    # Manifest is the single naming authority. Load it and resolve the data agent
    # name — pass the CLI display_name as command_name so any conflict raises
    # NameAuthorityConflict (hard fail, never silent override).
    if deploy_manifest_path:
        from fabric_kg_builder.deploy.manifest import (  # noqa: PLC0415
            load_deployment_manifest,
            DeploymentManifestError,
        )
        from fabric_kg_builder.deploy.name_authority import (  # noqa: PLC0415
            resolve_item_name,
        )
        try:
            _dk_manifest = load_deployment_manifest(deploy_manifest_path)
        except DeploymentManifestError as exc:
            raise BuildDeployError(
                f"Could not load deployment manifest for data-agent resolution: {exc}"
            ) from exc
        # NameAuthorityConflict propagates as-is — hard fail per ADR.
        _dk_resolved = resolve_item_name(
            _dk_manifest,
            "data_agent",
            command_name=data_agent_display_name or None,
        )
        configured_name = _dk_resolved.display_name
    if data_agent_mode not in {"update", "create", "replace"}:
        raise BuildDeployError(
            f"Unsupported Data Agent target mode: {data_agent_mode!r}."
        )
    if not configured_name:
        raise BuildDeployError("Data Agent display name must not be empty.")
    if data_agent_mode == "create" and configured_item_id:
        raise BuildDeployError(
            "Data Agent create mode cannot use a configured item ID. "
            "Choose update or approved replace."
        )
    if data_agent_mode in {"update", "replace"} and not configured_item_id:
        raise BuildDeployError(
            f"Data Agent {data_agent_mode} mode requires --data-agent-id "
            "or fabric.data_agent_item_id."
        )

    source_name = f"fkg-{run_token}-search-source"
    kb_name = f"fkg-{run_token}-knowledge-base"
    data_agent_name = configured_name
    credential = DefaultAzureCredential()
    _bd_competency_contract_exists = False
    _bd_competency_payload: dict[str, Any] = {}
    _bd_example_receipts: list[Any] = []
    _bd_example_direct_results: dict[str, dict[str, Any]] = {}
    _bd_example_candidate_count = 0
    try:
        loaded_semantic = load_semantic_model_artifacts(semantic_dir)
        projection_receipt = PersistedProjectionReceipt.model_validate_json(
            persisted_projection_path.read_text(encoding="utf-8")
        )
        projection_receipt_hash = (
            "sha256:"
            + hashlib.sha256(
                persisted_projection_path.read_bytes()
            ).hexdigest()
        )
        semantic_context = json.loads(
            semantic_context_path.read_text(encoding="utf-8")
        )
        if not isinstance(semantic_context, dict):
            raise ValueError("Agent semantic context must be an object.")
        agent_instructions = build_contract_agent_instructions(
            semantic_context,
            competency_questions=domain_contract.competency_questions,
            domain_context=_domain_instruction(domain_contract),
        )
        packaged_agent_instructions = agent_instructions_path.read_text(
            encoding="utf-8"
        )
        if agent_instructions != packaged_agent_instructions:
            raise AgentPublicationError(
                "AGENT_PACKAGE_INSTRUCTION_DRIFT",
                "Post-read-back instruction differs from the packaged instruction.",
            )
        package_instruction_hash = (
            "sha256:"
            + hashlib.sha256(
                packaged_agent_instructions.encode("utf-8")
            ).hexdigest()
        )
        grounding = build_persisted_agent_grounding(
            manifest=loaded_semantic.manifest,
            crosswalk=loaded_semantic.crosswalk,
            semantic_context=semantic_context,
            projection_receipt=projection_receipt,
            projection_receipt_hash=projection_receipt_hash,
            workspace_id=workspace_id,
            graph_model_id=graph_model_id,
        )
        public_elements, public_metadata = (
            build_public_ontology_source_projection(grounding)
        )
        graph_elements, graph_metadata = (
            build_public_graph_source_projection(grounding)
        )
        competency_path = semantic_context_path.parent / "competency-contract.json"
        _bd_competency_contract_exists = competency_path.exists()
        _bd_competency_payload = (
            json.loads(competency_path.read_text(encoding="utf-8"))
            if _bd_competency_contract_exists
            else {}
        )
        # Build availability dict from materialization plan for capability-aware
        # example gating (#13).
        _bd_availability = {
            item.semantic_id: item
            for item in loaded_semantic.materialization_plan.data_availability
        }
        _bd_graph_client = GraphModelGQLClient(
            token_provider=lambda: credential.get_token(
                "https://api.fabric.microsoft.com/.default"
            ).token,
        )

        def _bd_graph_executor(query: str) -> dict[str, Any]:
            return _bd_graph_client.execute_query_all_pages(
                workspace_id,
                graph_model_id,
                query,
            )

        _bd_example_validation = validate_graph_few_shot_examples(
            _bd_competency_payload,
            availability=_bd_availability if _bd_availability else None,
            limit=7,
            dry_run=False,
            execute_graph_query=_bd_graph_executor,
            query_schema=_bd_competency_payload.get("query_schema"),
            require_schema=_bd_competency_contract_exists,
        )
        graph_few_shots = _bd_example_validation.examples
        _bd_example_receipts = _bd_example_validation.receipts
        _bd_example_direct_results = _bd_example_validation.direct_results
        _bd_example_candidate_count = _bd_example_validation.candidate_count
        data_agent_spec = DataAgentSpec(
            display_name=data_agent_name,
            instruction=agent_instructions,
            sources=[
                DataSourceSpec(
                    source_type="ontology",
                    name=str(
                        fabric.get("ontology_display_name")
                        or ontology_item_id
                    ),
                    artifact_id=ontology_item_id,
                    workspace_id=workspace_id,
                    display_name=str(
                        fabric.get("ontology_display_name")
                        or ontology_item_id
                    ),
                    instructions=build_ontology_source_instructions(
                        semantic_context
                    ),
                    description=build_ontology_source_description(
                        semantic_context
                    ),
                    metadata=public_metadata,
                    elements=list(public_elements),
                    preview=True,
                ),
                DataSourceSpec(
                    source_type="graph",
                    name=str(
                        fabric.get("graph_model_display_name")
                        or graph_model_id
                    ),
                    artifact_id=graph_model_id,
                    workspace_id=workspace_id,
                    display_name=str(
                        fabric.get("graph_model_display_name")
                        or graph_model_id
                    ),
                    instructions=build_graph_source_instructions(
                        semantic_context,
                        availability=_bd_availability or None,
                    ),
                    description=build_graph_source_description(
                        semantic_context,
                        availability=_bd_availability or None,
                    ),
                    metadata=graph_metadata,
                    elements=list(graph_elements),
                    few_shots=graph_few_shots,
                ),
            ],
        )
    except DataAgentRequiredExampleEmpty as exc:
        raise BuildDeployError(str(exc)) from exc
    except DataAgentExampleValidationFailed as exc:
        raise BuildDeployError(str(exc)) from exc
    except (
        AgentPublicationError,
        OSError,
        ValueError,
    ) as exc:
        raise BuildDeployError(
            f"Data Agent grounding preparation failed: {exc}"
        ) from exc

    from fabric_kg_builder.knowledge.validation import (  # noqa: PLC0415
        FewShotContractViolation,
        SourcePolicy,
        SourcePolicyViolation,
        TextLimitViolation,
        validate_data_agent_text,
        validate_graph_few_shots,
        validate_instruction_deduplication,
        validate_source_policy,
    )

    _bd_source_policy = SourcePolicy(
        required=frozenset({"ontology", "graph"}),
    )
    try:
        validate_source_policy(data_agent_spec, _bd_source_policy)
    except SourcePolicyViolation as exc:
        raise BuildDeployError(str(exc)) from exc

    try:
        validate_graph_few_shots(
            data_agent_spec, contract_exists=_bd_competency_contract_exists
        )
    except FewShotContractViolation as exc:
        raise BuildDeployError(str(exc)) from exc

    text_results = validate_data_agent_text(data_agent_spec)
    dedup_violations = validate_instruction_deduplication(data_agent_spec)
    text_failures = [r for r in text_results if not r.passed]
    if text_failures:
        first = text_failures[0]
        raise BuildDeployError(
            TextLimitViolation(
                field=first.field,
                actual=first.actual,
                limit=first.limit,
                remediation=first.remediation,
            ).args[0]
        )
    if dedup_violations:
        raise BuildDeployError(
            "Duplicate instruction blocks detected:\n"
            + "\n".join(f"  - {v}" for v in dedup_violations)
        )

    click.echo(
        "[build-deploy] Data Agent target: "
        f"workspace={workspace_name} ({workspace_id}), "
        f"agent={data_agent_name} "
        f"({configured_item_id or 'new item'}), "
        f"action={data_agent_mode}, source=Ontology {ontology_item_id}, "
        f"instruction={package_instruction_hash}, "
        f"selection={stage_snapshot_from_spec(data_agent_spec).source_selection_hash}"
    )

    # Report source policy and text validation counts
    _source_type_label_bd = {"ontology": "required ✓", "graph": "required ✓"}
    click.echo("[build-deploy] Source policy:")
    for _src in data_agent_spec.sources:
        _label = _source_type_label_bd.get(_src.source_type, "present")
        click.echo(f"  {_src.source_type}: {_label}")
    click.echo("[build-deploy] Source policy: PASS")
    click.echo("[build-deploy] Definition text validation:")
    for _r in text_results:
        click.echo(f"  {_r.field}: {_r.actual:,} / {_r.limit:,}")
    click.echo(f"  duplicate instruction blocks: {len(dedup_violations)}")
    click.echo("[build-deploy] Definition text policy: PASS")

    # Capability reporting (#12 — property selection + grounding text counts)
    _bd_stage_snap = stage_snapshot_from_spec(data_agent_spec)
    _bd_req_prop = grounding.expected_property_child_count
    _bd_comp_prop = _bd_stage_snap.property_child_count
    _bd_prop_pct = (
        int(_bd_comp_prop / _bd_req_prop * 100) if _bd_req_prop > 0 else 100
    )
    click.echo("[build-deploy] Property selection:")
    click.echo(f"  required by semantic contract: {_bd_req_prop:,}")
    click.echo(f"  selected in compiled spec:     {_bd_comp_prop:,}")
    click.echo(f"  Property coverage: {_bd_prop_pct}%")
    _bd_global_instr_chars = len(str(data_agent_spec.instruction or ""))
    _bd_graph_src = next(
        (s for s in data_agent_spec.sources if str(s.source_type) == "graph"),
        None,
    )
    _bd_ontology_src = next(
        (s for s in data_agent_spec.sources if str(s.source_type) == "ontology"),
        None,
    )
    _bd_graph_instr_chars = (
        len(str(_bd_graph_src.instructions or "")) if _bd_graph_src else 0
    )
    _bd_graph_desc_chars = (
        len(str(_bd_graph_src.description or "")) if _bd_graph_src else 0
    )
    _bd_ontology_instr_chars = (
        len(str(_bd_ontology_src.instructions or "")) if _bd_ontology_src else 0
    )
    _bd_ontology_desc_chars = (
        len(str(_bd_ontology_src.description or "")) if _bd_ontology_src else 0
    )
    _bd_instruction_chars: dict[str, int] = {}
    _bd_description_chars: dict[str, int] = {}
    if _bd_graph_src:
        _bd_instruction_chars["graph"] = _bd_graph_instr_chars
        _bd_description_chars["graph"] = _bd_graph_desc_chars
    if _bd_ontology_src:
        _bd_instruction_chars["ontology"] = _bd_ontology_instr_chars
        _bd_description_chars["ontology"] = _bd_ontology_desc_chars
    click.echo("[build-deploy] Grounding text:")
    click.echo(f"  global instructions:      {_bd_global_instr_chars:,} chars")
    click.echo(
        f"  ontology description:     {_bd_ontology_desc_chars:,} chars"
    )
    click.echo(f"  graph description:        {_bd_graph_desc_chars:,} chars")

    # Graph example validation reporting (#11/#13)
    if _bd_competency_contract_exists:
        click.echo("[build-deploy] Graph example validation:")
        click.echo(f"  candidates discovered: {_bd_example_candidate_count}")
        for receipt in _bd_example_receipts:
            status = "PASS" if receipt.published else "OMIT"
            rows = receipt.direct_graph_row_count or 0
            evidence = (
                f"{int(round(receipt.evidence_coverage * 100)):d}%"
                if receipt.evidence_coverage > 0
                else "0%"
            )
            click.echo(
                f"  {status:4} {receipt.competency_id} "
                f"rows={rows} evidence={evidence}"
            )
        click.echo(f"  examples selected: {len(graph_few_shots)} / 7")

    capability = CapabilityResult(
        endpoint=search_endpoint,
        api_version=_GA_VERSION,
        available_features=_GA_FEATURES,
    )
    search_client = SearchKbClient(
        capability=capability,
        transport=RequestsTransport(),
    )
    source_result = search_client.upsert_knowledge_source(
        SearchIndexKnowledgeSourceSpec(
            name=source_name,
            search_index_name=search_index_name,
            semantic_configuration_name="kg-chunks-semantic",
            source_data_fields=[
                "chunk_id",
                "asset_id",
                "asset_version_id",
                "run_id",
                "source_locator_json",
                "source_path",
            ],
            search_fields=["content", "entity_aliases"],
            description=(
                f"Traceable source for {domain_contract.domain.name} evidence."
            ),
        )
    )
    kb_result = search_client.upsert_knowledge_base(
        KnowledgeBaseSpec(
            name=kb_name,
            knowledge_source_names=[source_name],
            description=(
                f"Hybrid and semantic retrieval for "
                f"{domain_contract.domain.name}."
            ),
        )
    )

    fabric_token = credential.get_token(
        "https://api.fabric.microsoft.com/.default"
    ).token
    data_agent_client = FabricDataAgentClient(
        workspace_id=workspace_id,
        transport=RequestsTransport(),
        token=fabric_token,
    )
    try:
        (
            data_agent_result,
            publish_result,
            agent_publication_receipt,
        ) = deploy_and_validate_data_agent(
            client=data_agent_client,
            spec=data_agent_spec,
            target_mode=data_agent_mode,
            configured_target_item_id=configured_item_id or None,
            replace_approved=approve_data_agent_replace,
            workspace_name=workspace_name,
            workspace_id=workspace_id,
            package_instruction_hash=package_instruction_hash,
            grounding=grounding,
            projection_receipt=projection_receipt,
            projection_receipt_hash=projection_receipt_hash,
            published_description=(
                f"{data_agent_name} semantic release for run {run_token}."
            ),
            required_source_type="graph",
            source_policy=_bd_source_policy,
            global_instruction_chars=_bd_global_instr_chars,
            instruction_chars=_bd_instruction_chars,
            description_chars=_bd_description_chars,
            competency_examples=_bd_example_receipts,
        )
        if _bd_competency_contract_exists and _bd_example_receipts:
            from fabric_kg_builder.runtime.contract import CompetencyCase
            from fabric_kg_builder.runtime.executors import DataAgentMcpExecutor

            mcp_endpoint = (
                "https://api.fabric.microsoft.com/v1/workspaces/"
                f"{workspace_id}/dataagents/{data_agent_result.item_id}/agent"
            )
            mcp_executor = DataAgentMcpExecutor(
                endpoint=mcp_endpoint,
                token_provider=lambda: credential.get_token(
                    "https://api.fabric.microsoft.com/.default"
                ).token,
            )

            def _execute_data_agent_case(case_payload: dict[str, Any]) -> dict[str, Any]:
                case_model = CompetencyCase.model_validate(case_payload)
                return mcp_executor.execute(case_model)

            _bd_example_receipts = compare_graph_few_shot_semantics(
                _bd_competency_payload,
                _bd_example_receipts,
                direct_results=_bd_example_direct_results,
                execute_data_agent_case=_execute_data_agent_case,
            )
            agent_publication_receipt = agent_publication_receipt.model_copy(
                update={"competency_examples": _bd_example_receipts}
            )
    except (
        AgentPublicationError,
        DataAgentExampleValidationFailed,
        DataAgentDefinitionError,
        DataAgentTargetError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise BuildDeployError(
            f"Exact Data Agent publication failed: {exc}"
        ) from exc
    _atomic_json(
        agent_publication_receipt_path,
        agent_publication_receipt.model_dump(mode="json"),
    )
    _persist_data_agent_identity(
        environment,
        item_id=data_agent_result.item_id,
        display_name=data_agent_name,
    )

    account_name = str(
        outputs.get("foundryAccountName")
        or str(outputs.get("foundryAccountId", "")).rstrip("/").rsplit("/", 1)[-1]
    )
    project_name = str(
        outputs.get("foundryProjectName")
        or manifest.resources.foundry.project_name
    )
    connection_client = FoundryProjectConnectionClient(
        subscription_id=manifest.azure.subscription_id,
        resource_group=str(manifest.azure.resource_group.name or ""),
        account_name=account_name,
        project_name=project_name,
        credential=credential,
    )
    data_agent_connection = connection_client.upsert_fabric_data_agent(
        name=f"fkg-{run_token}-fabric-agent",
        workspace_id=workspace_id,
        data_agent_id=data_agent_result.item_id,
    )
    search_connection_id = str(outputs.get("foundrySearchConnectionId") or "")
    if require_foundry_search_connection and not search_connection_id:
        raise BuildDeployError(
            "Foundry Azure AI Search project connection ID is missing."
        )
    _update_grounding_metadata(
        metadata_path=metadata_path,
        environment=environment,
        search_connection_id=search_connection_id,
        search_index_name=search_index_name,
        data_agent_connection_id=data_agent_connection.resource_id,
        knowledge_base_name=kb_name,
    )
    return {
        "knowledge_source_name": source_result.name,
        "knowledge_source_created": source_result.created,
        "knowledge_base_name": kb_result.name,
        "knowledge_base_created": kb_result.created,
        "knowledge_base_id": f"{search_endpoint}/knowledgebases/{kb_name}",
        "data_agent_name": data_agent_name,
        "data_agent_id": data_agent_result.item_id,
        "data_agent_workspace_name": workspace_name,
        "data_agent_workspace_id": workspace_id,
        "data_agent_target_mode": data_agent_mode,
        "data_agent_actions": agent_publication_receipt.actions,
        "data_agent_published": True,
        "published_description": publish_result.published_description,
        "selected_source_names": [
            source.source_name
            for source in agent_publication_receipt.selected_sources
        ],
        "selected_source_ids": [
            source.artifact_id
            for source in agent_publication_receipt.selected_sources
        ],
        "fabric_data_agent_connection_id": data_agent_connection.resource_id,
        "semantic_contract_hash": (
            loaded_semantic.manifest.semantic_contract_hash
        ),
        "semantic_model_manifest_hash": (
            agent_publication_receipt.semantic_model_manifest_hash
        ),
        "compiled_instruction_hash": (
            agent_publication_receipt.compiled_instruction_hash
        ),
        "deployed_instruction_hash": (
            agent_publication_receipt.published_instruction_hash
        ),
        "source_selection_hash": (
            agent_publication_receipt.published_source_selection_hash
        ),
        "selected_element_hash": (
            agent_publication_receipt.published_selected_element_hash
        ),
        "agent_schema_sidecar_hash": (
            agent_publication_receipt.agent_schema_sidecar_hash
        ),
        "agent_publication_receipt_path": str(
            agent_publication_receipt_path
        ),
        "agent_publication_receipt_hash": (
            "sha256:"
            + hashlib.sha256(
                agent_publication_receipt_path.read_bytes()
            ).hexdigest()
        ),
    }


def _resource_kind(resource_type: str) -> str:
    mapping = {
        "Microsoft.Storage/storageAccounts": "azure_blob_storage",
        "Microsoft.CognitiveServices/accounts/document-intelligence": (
            "azure_document_intelligence"
        ),
        "Microsoft.CognitiveServices/accounts/foundry": (
            "azure_foundry_resource"
        ),
        "Microsoft.CognitiveServices/accounts/projects": (
            "azure_foundry_project"
        ),
        "Microsoft.CognitiveServices/accounts/deployments/chat": (
            "azure_foundry_model_deployment"
        ),
        "Microsoft.CognitiveServices/accounts/deployments/embedding": (
            "azure_foundry_model_deployment"
        ),
        "Microsoft.CognitiveServices/accounts/projects/connections/search": (
            "foundry_project_connection"
        ),
        "Microsoft.Search/searchServices": "azure_ai_search",
        "Microsoft.ContainerRegistry/registries": "azure_container_registry",
        "Microsoft.ManagedIdentity/userAssignedIdentities": (
            "azure_managed_identity"
        ),
        "Fabric/Workspace": "fabric_workspace",
        "Fabric/Lakehouse": "fabric_lakehouse",
        "Fabric/Ontology": "fabric_ontology",
        "Fabric/GraphModel": "fabric_graph_model",
    }
    return mapping.get(resource_type, resource_type.lower().replace("/", "_"))


def _ledger_record(
    *,
    kind: str,
    resource_id: str,
    display_name: str,
    adoption_mode: str,
    run_id: str,
) -> dict[str, Any]:
    return {
        "resource_kind": kind,
        "arm_or_fabric_id": resource_id,
        "display_name": display_name,
        "adoption_mode": adoption_mode,
        "status": "active",
        "tags": {
            "fabric_kg_run_id": run_id,
            "run_id": run_id,
        },
    }


def _build_resource_ledger(
    *,
    run_id: str,
    environment: str,
    run_root: Path,
    outputs: dict[str, Any],
    manifest: Any,
    state: _RunState,
    search_index_name: str,
) -> dict[str, Any]:
    infra_state_path = run_root / "infra" / environment / "state.json"
    resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    fabric_display_names = {
        "Fabric/Workspace": (
            manifest.fabric.workspace.display_name
            or manifest.fabric.workspace.name
            or "workspace"
        ),
        "Fabric/Lakehouse": (
            manifest.fabric.lakehouse.display_name
            or manifest.fabric.lakehouse.name
            or "lakehouse"
        ),
        "Fabric/Ontology": (
            manifest.fabric.ontology.display_name
            or manifest.fabric.ontology.name
            or "ontology"
        ),
        "Fabric/GraphModel": (
            manifest.fabric.graph_model.display_name
            or manifest.fabric.graph_model.name
            or "graph"
        ),
    }
    if infra_state_path.exists():
        infra_state = json.loads(infra_state_path.read_text(encoding="utf-8"))
        for mode, key in (
            ("create", "managed_resource_ids"),
            ("connect", "adopted_resource_ids"),
        ):
            for resource_type, resource_id in (
                infra_state.get(key) or {}
            ).items():
                resource_id = str(resource_id)
                if not resource_id or resource_id in seen:
                    continue
                seen.add(resource_id)
                resources.append(
                    _ledger_record(
                        kind=_resource_kind(resource_type),
                        resource_id=resource_id,
                        display_name=str(
                            fabric_display_names.get(resource_type)
                            or resource_id.rstrip("/").rsplit("/", 1)[-1]
                        ),
                        adoption_mode=mode,
                        run_id=run_id,
                    )
                )

    output_resources = [
        (
            "storageAccountId",
            "Microsoft.Storage/storageAccounts",
            manifest.resources.storage.mode.value,
            outputs.get("storageAccountName"),
        ),
        (
            "documentIntelligenceId",
            "Microsoft.CognitiveServices/accounts/document-intelligence",
            manifest.resources.document_intelligence.mode.value,
            outputs.get("documentIntelligenceName"),
        ),
        (
            "foundryAccountId",
            "Microsoft.CognitiveServices/accounts/foundry",
            manifest.resources.foundry.mode.value,
            outputs.get("foundryAccountName"),
        ),
        (
            "foundryProjectId",
            "Microsoft.CognitiveServices/accounts/projects",
            "create",
            outputs.get("foundryProjectName"),
        ),
        (
            "chatDeploymentId",
            "Microsoft.CognitiveServices/accounts/deployments/chat",
            "create",
            outputs.get("chatDeploymentName"),
        ),
        (
            "embeddingDeploymentId",
            "Microsoft.CognitiveServices/accounts/deployments/embedding",
            "create",
            outputs.get("embeddingDeploymentName"),
        ),
        (
            "foundrySearchConnectionId",
            "Microsoft.CognitiveServices/accounts/projects/connections/search",
            "create",
            outputs.get("foundrySearchConnectionName"),
        ),
        (
            "searchServiceId",
            "Microsoft.Search/searchServices",
            manifest.resources.search.mode.value,
            outputs.get("searchServiceName"),
        ),
        (
            "containerRegistryId",
            "Microsoft.ContainerRegistry/registries",
            manifest.resources.container_registry.mode.value,
            outputs.get("containerRegistryName"),
        ),
        (
            "identityId",
            "Microsoft.ManagedIdentity/userAssignedIdentities",
            "create",
            outputs.get("identityName"),
        ),
        (
            "fabricWorkspaceId",
            "Fabric/Workspace",
            manifest.fabric.workspace.mode.value,
            fabric_display_names["Fabric/Workspace"],
        ),
        (
            "fabricLakehouseId",
            "Fabric/Lakehouse",
            manifest.fabric.lakehouse.mode.value,
            fabric_display_names["Fabric/Lakehouse"],
        ),
        (
            "fabricOntologyId",
            "Fabric/Ontology",
            manifest.fabric.ontology.mode.value,
            fabric_display_names["Fabric/Ontology"],
        ),
        (
            "fabricGraphModelId",
            "Fabric/GraphModel",
            manifest.fabric.graph_model.mode.value,
            fabric_display_names["Fabric/GraphModel"],
        ),
    ]
    for output_key, resource_type, mode, configured_name in output_resources:
        resource_id = str(outputs.get(output_key) or "")
        if not resource_id or resource_id in seen:
            continue
        seen.add(resource_id)
        resources.append(
            _ledger_record(
                kind=_resource_kind(resource_type),
                resource_id=resource_id,
                display_name=str(
                    configured_name
                    or resource_id.rstrip("/").rsplit("/", 1)[-1]
                ),
                adoption_mode=mode,
                run_id=run_id,
            )
        )

    storage_account_id = str(outputs.get("storageAccountId") or "").rstrip("/")
    container_name = str(outputs.get("containerName") or "")
    if (
        storage_account_id
        and container_name
        and manifest.resources.storage.mode.value == "create"
    ):
        container_id = (
            f"{storage_account_id}/blobServices/default/containers/{container_name}"
        )
        if container_id not in seen:
            seen.add(container_id)
            resources.append(
                _ledger_record(
                    kind="azure_blob_container",
                    resource_id=container_id,
                    display_name=container_name,
                    adoption_mode="create",
                    run_id=run_id,
                )
            )

    serving_receipt_path = run_root / "release" / "serving-deployment.json"
    serving_receipt = (
        json.loads(serving_receipt_path.read_text(encoding="utf-8"))
        if serving_receipt_path.exists()
        else {}
    )
    ontology_receipt_path = run_root / "release" / "ontology-deployment.json"
    ontology_receipt = (
        json.loads(ontology_receipt_path.read_text(encoding="utf-8"))
        if ontology_receipt_path.exists()
        else {}
    )
    for receipt, id_key, kind, display_name, mode in (
        (
            serving_receipt,
            "graph_model_id",
            "fabric_graph_model",
            str(
                manifest.fabric.graph_model.display_name
                or manifest.fabric.graph_model.name
                or "graph"
            ),
            manifest.fabric.graph_model.mode.value,
        ),
        (
            ontology_receipt,
            "ontology_item_id",
            "fabric_ontology",
            str(
                manifest.fabric.ontology.display_name
                or manifest.fabric.ontology.name
                or "ontology"
            ),
            manifest.fabric.ontology.mode.value,
        ),
    ):
        resource_id = str(receipt.get(id_key) or "")
        if not resource_id or resource_id in seen:
            continue
        seen.add(resource_id)
        resources.append(
            _ledger_record(
                kind=kind,
                resource_id=resource_id,
                display_name=display_name,
                adoption_mode=mode,
                run_id=run_id,
            )
        )

    search_endpoint = str(outputs.get("searchEndpoint") or "").rstrip("/")
    if (
        search_endpoint
        and state.data.get("stages", {})
        .get("compile_search", {})
        .get("status")
        == "succeeded"
    ):
        physical_index_name = str(
            serving_receipt.get("physical_index_name") or search_index_name
        )
        resources.append(
            _ledger_record(
                kind="azure_ai_search_index",
                resource_id=f"{search_endpoint}/indexes/{physical_index_name}",
                display_name=physical_index_name,
                adoption_mode="create",
                run_id=run_id,
            )
        )

    knowledge = (
        state.data.get("stages", {})
        .get("deploy_knowledge", {})
        .get("details", {})
    )
    if knowledge:
        resources.extend(
            [
                _ledger_record(
                    kind="foundry_knowledge_source",
                    resource_id=(
                        f"{search_endpoint}/knowledgeSources/"
                        f"{knowledge['knowledge_source_name']}"
                    ),
                    display_name=str(knowledge["knowledge_source_name"]),
                    adoption_mode="create",
                    run_id=run_id,
                ),
                _ledger_record(
                    kind="foundry_knowledge_base",
                    resource_id=str(knowledge["knowledge_base_id"]),
                    display_name=str(knowledge["knowledge_base_name"]),
                    adoption_mode="create",
                    run_id=run_id,
                ),
                _ledger_record(
                    kind="fabric_data_agent",
                    resource_id=str(knowledge["data_agent_id"]),
                    display_name=str(knowledge["data_agent_name"]),
                    adoption_mode=str(
                        knowledge.get("data_agent_target_mode")
                        or "create"
                    ),
                    run_id=run_id,
                ),
                _ledger_record(
                    kind="foundry_project_connection",
                    resource_id=str(
                        knowledge["fabric_data_agent_connection_id"]
                    ),
                    display_name=str(
                        knowledge["fabric_data_agent_connection_id"]
                    ).rstrip("/").rsplit("/", 1)[-1],
                    adoption_mode="create",
                    run_id=run_id,
                ),
            ]
        )

    agent = (
        state.data.get("stages", {})
        .get("deploy_agent", {})
        .get("details", {})
    )
    if agent.get("agent_version_id"):
        resources.append(
            _ledger_record(
                kind="foundry_agent",
                resource_id=str(agent["agent_version_id"]),
                display_name=str(agent["agent_name"]),
                adoption_mode="create",
                run_id=run_id,
            )
        )

    receipt_path = run_root / "release" / "app-deployment.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        app_outputs = receipt.get("outputs") or {}
        for key, kind in (
            ("apiContainerAppId", "azure_container_app"),
            ("uiContainerAppId", "azure_container_app"),
            ("containerAppsEnvId", "azure_container_apps_environment"),
            ("logAnalyticsWorkspaceId", "azure_log_analytics"),
        ):
            resource_id = str(app_outputs.get(key) or "")
            if resource_id:
                resources.append(
                    _ledger_record(
                        kind=kind,
                        resource_id=resource_id,
                        display_name=resource_id.rstrip("/").rsplit("/", 1)[-1],
                        adoption_mode="create",
                        run_id=run_id,
                    )
                )

    ledger = {
        "schema": _LEDGER_SCHEMA,
        "run_id": run_id,
        "environment": environment,
        "created_at": _utc_now(),
        "resources": resources,
    }
    _atomic_json(run_root / "release" / "ledger.json", ledger)
    return ledger


def _prepare_runtime_acceptance_config(
    *,
    template_path: Path,
    target_path: Path,
    receipt_path: Path,
    environment: str,
    outputs: dict[str, Any],
    state: _RunState,
) -> dict[str, Any]:
    try:
        payload = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildDeployError(
            f"Could not read runtime config template {template_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise BuildDeployError(
            "Runtime config template must contain a JSON object."
        )
    receipt = _read_json_object(receipt_path)
    competency_hash = receipt.get("competency_contract_hash")
    if not competency_hash:
        raise BuildDeployError(
            "Deployment receipt has no competency contract hash. "
            "Pass --competency-suite when building."
        )
    instruction_hash = receipt.get("instruction_hash")
    deployed_instruction_hash = receipt.get(
        "deployed_instruction_hash"
    )
    if not instruction_hash:
        raise BuildDeployError(
            "Deployment receipt has no compiled instruction hash."
        )
    if not deployed_instruction_hash:
        raise BuildDeployError(
            "Deployment receipt has no independently read deployed "
            "instruction hash."
        )
    for field, label in (
        ("graph_model_id", "Graph Model ID"),
        ("search_index_name", "Search index name"),
        ("data_agent_id", "Data Agent ID"),
    ):
        if not receipt.get(field):
            raise BuildDeployError(
                f"Deployment receipt has no {label}; runtime acceptance "
                "requires completed serving and knowledge deployment."
            )

    payload["environment"] = environment
    payload["contract_hash"] = competency_hash
    deployment = payload.setdefault("deployment", {})
    if not isinstance(deployment, dict):
        raise BuildDeployError(
            "Runtime config deployment section must be an object."
        )
    deployment.update(
        {
            "artifact_validation_status": "passed",
            "data_agent_published": (
                receipt.get("data_agent_published") is True
            ),
            "compiled_instruction_hash": instruction_hash,
            "deployed_instruction_hash": deployed_instruction_hash,
            "receipt_path": str(receipt_path.resolve()),
        }
    )

    graph = payload.get("graph")
    if isinstance(graph, dict):
        graph["workspace_id"] = str(
            outputs.get("fabricWorkspaceId") or graph.get("workspace_id") or ""
        )
        graph["graph_model_id"] = str(
            receipt.get("graph_model_id") or graph.get("graph_model_id") or ""
        )

    search = payload.get("search")
    if isinstance(search, dict):
        search["endpoint"] = str(
            outputs.get("searchEndpoint") or search.get("endpoint") or ""
        ).rstrip("/")
        embedding_endpoint = str(
            outputs.get("foundryOpenAIEndpoint")
            or outputs.get("foundryProjectEndpoint")
            or search.get("embedding_endpoint")
            or ""
        ).rstrip("/")
        if embedding_endpoint:
            search["embedding_endpoint"] = embedding_endpoint
        embedding_deployment = str(
            outputs.get("embeddingDeploymentName")
            or search.get("embedding_deployment")
            or ""
        ).strip()
        if embedding_deployment:
            search["embedding_deployment"] = embedding_deployment
        search.setdefault("embedding_dimensions", 1536)
        if search.get("mode", "direct_search") == "direct_search":
            search["index_name"] = str(
                receipt.get("search_index_name")
                or search.get("index_name")
                or ""
            )
        else:
            knowledge = (
                state.data.get("stages", {})
                .get("deploy_knowledge", {})
                .get("details", {})
            )
            if isinstance(knowledge, dict):
                search["knowledge_base_name"] = str(
                    knowledge.get("knowledge_base_name")
                    or search.get("knowledge_base_name")
                    or ""
                )
                search["knowledge_base_id"] = str(
                    receipt.get("knowledge_base_id")
                    or search.get("knowledge_base_id")
                    or ""
                )

    data_agent_mcp = payload.get("data_agent_mcp")
    if isinstance(data_agent_mcp, dict):
        workspace_id = str(
            outputs.get("fabricWorkspaceId")
            or data_agent_mcp.get("workspace_id")
            or ""
        )
        data_agent_id = str(
            receipt.get("data_agent_id")
            or data_agent_mcp.get("data_agent_id")
            or ""
        )
        data_agent_mcp["workspace_id"] = workspace_id
        data_agent_mcp["data_agent_id"] = data_agent_id
        data_agent_mcp.setdefault("request_timeout_seconds", 120)
        data_agent_mcp["endpoint"] = (
            "https://api.fabric.microsoft.com/v1/mcp/workspaces/"
            f"{workspace_id}/dataagents/{data_agent_id}/agent"
        )

    _atomic_json(target_path, payload)
    from fabric_kg_builder.runtime import load_runtime_config

    config = load_runtime_config(target_path)
    return {
        "path": str(target_path),
        "contract_hash": config.contract_hash,
        "receipt_sha256": config.deployment.receipt_sha256,
        "graph_model_id": (
            config.graph.graph_model_id if config.graph else None
        ),
        "search_index_name": (
            config.search.index_name if config.search else None
        ),
        "data_agent_id": (
            config.data_agent_mcp.data_agent_id
            if config.data_agent_mcp
            else None
        ),
    }


def _run_runtime_acceptance(
    *,
    competency_contract_path: Path,
    runtime_config_path: Path,
    release_dir: Path,
) -> dict[str, Any]:
    from fabric_kg_builder.runtime import (
        build_live_collector,
        build_runtime_report,
        evaluate_runtime_evidence,
        load_competency_contract,
        load_runtime_config,
        validate_deployment_evidence,
    )

    contract = load_competency_contract(competency_contract_path)
    config = load_runtime_config(runtime_config_path)
    evidence = build_live_collector(
        contract=contract,
        config=config,
    ).collect()
    validation = validate_deployment_evidence(evidence)
    evaluation = evaluate_runtime_evidence(evidence)
    report = build_runtime_report(
        evidence,
        deployment_validation=validation,
        evaluation=evaluation,
    )
    evidence_path = release_dir / "runtime-evidence.json"
    validation_path = release_dir / "deployment-validation.json"
    evaluation_path = release_dir / "runtime-evaluation.json"
    report_path = release_dir / "runtime-report.json"
    _atomic_json(evidence_path, evidence)
    _atomic_json(validation_path, validation)
    _atomic_json(evaluation_path, evaluation)
    _atomic_json(report_path, report)
    if report["status"] != "passed":
        raise BuildDeployError(
            "Runtime acceptance did not pass "
            f"(status={report['status']}). Report: {report_path}"
        )
    return {
        "status": report["status"],
        "evidence": str(evidence_path),
        "validation": str(validation_path),
        "evaluation": str(evaluation_path),
        "report": str(report_path),
    }


@click.command(
    "build-deploy",
    epilog=_BUILD_DEPLOY_EPILOG,
    context_settings={"max_content_width": 120},
)
@click.option(
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True),
    help="Source file or directory to process through the full pipeline.",
)
@click.option(
    "--domain-contract",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Approved domain.yaml contract used for extraction and validation.",
)
@click.option(
    "--env",
    required=True,
    type=str,
    help="Target deployment environment.",
)
@click.option("--run-id", default=None, help="Stable run UUID. Generated if omitted.")
@click.option(
    "--state-dir",
    default=None,
    type=click.Path(),
    help="Run-state root. The run UUID is appended unless already the final path segment.",
)
@click.option("--infra-dir", default="infra", show_default=True, type=click.Path())
@click.option("--provision/--no-provision", default=True, show_default=True)
@click.option(
    "--infra-outputs",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help=(
        "Existing non-secret outputs.json to import with --no-provision. "
        "Defaults to build/infra/<env>/outputs.json."
    ),
)
@click.option("--recursive", is_flag=True, default=False)
@click.option(
    "--drawing-mode",
    type=click.Choice(["auto", "always", "off"]),
    default="auto",
    show_default=True,
)
@click.option(
    "--densify-config",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Optional explicit domain-neutral densification rule set.",
)
@click.option(
    "--semantic-contract",
    required=False,
    default=None,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Approved canonical semantic contract YAML.",
)
@click.option(
    "--semantic-mappings",
    default="ontology/mappings.yaml",
    show_default=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Physical mappings for the canonical semantic contract.",
)
@click.option(
    "--semantic-vocabulary",
    default="ontology/vocabulary.yaml",
    show_default=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Controlled vocabulary for the canonical semantic contract.",
)
@click.option(
    "--semantic-ids-lock",
    default="ontology/ids.lock.json",
    show_default=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Stable semantic and Fabric ID lock.",
)
@click.option(
    "--competency-suite",
    default=None,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help=(
        "Route-aware competency YAML/JSON compiled into the agent package. "
        "Required before automated runtime acceptance."
    ),
)
@click.option(
    "--runtime-config",
    "runtime_config_template",
    default=None,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help=(
        "Secret-free runtime config template. When provided, build-deploy "
        "runs Graph, Search, and Data Agent MCP acceptance after deployment."
    ),
)
@click.option("--embed/--no-embed", default=True, show_default=True)
@click.option("--skip-search", is_flag=True, default=False)
@click.option("--deploy-serving/--no-deploy-serving", default=True, show_default=True)
@click.option("--deploy-knowledge", is_flag=True, default=False)
@click.option(
    "--data-agent-mode",
    type=click.Choice(["update", "create", "replace"]),
    default=None,
    help=(
        "Required with --deploy-knowledge: update the exact configured item, "
        "create one owned item, or replace the configured item with approval."
    ),
)
@click.option(
    "--data-agent-id",
    default=None,
    help=(
        "Exact Fabric Data Agent item ID for update/replace. Defaults to "
        "fabric.data_agent_item_id in the environment configuration."
    ),
)
@click.option(
    "--data-agent-name",
    default=None,
    help=(
        "Stable Data Agent display name. Defaults to configured identity or "
        "fkg-<environment>-data-agent."
    ),
)
@click.option(
    "--approve-data-agent-replace",
    is_flag=True,
    default=False,
    help="Approve deletion and replacement of the exact configured Data Agent.",
)
@click.option("--deploy-agent", is_flag=True, default=False)
@click.option("--deploy-app", is_flag=True, default=False)
@click.option("--graph-preview-acknowledged", is_flag=True, default=False)
@click.option(
    "--approve-breaking-semantic-migration",
    is_flag=True,
    default=False,
    help=(
        "Explicitly approve a classified breaking semantic migration before "
        "live Ontology or Graph mutation."
    ),
)
@click.option(
    "--previous-semantic-contract",
    default=None,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help=(
        "Approved contract currently deployed to the target. Use this when "
        "the local semantic baseline is unavailable."
    ),
)
@click.option(
    "--initialize-semantic-baseline",
    is_flag=True,
    default=False,
    help=(
        "Confirm that the target has no prior semantic deployment and allow "
        "the current contract to establish its first local baseline."
    ),
)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--resume", is_flag=True, default=False)
@click.option("--force", is_flag=True, default=False)
@click.option("--manifest", "deploy_manifest_path", default=None, type=click.Path(),
              help="Path to deployment.yaml (naming authority for all Fabric items). "
                   "Defaults to env-config names (legacy mode).")
@click.pass_context
def build_deploy_cmd(
    ctx: click.Context,
    input_path: str,
    domain_contract: str,
    env: str,
    run_id: str | None,
    state_dir: str | None,
    infra_dir: str,
    provision: bool,
    infra_outputs: Path | None,
    recursive: bool,
    drawing_mode: str,
    densify_config: str | None,
    semantic_contract: Path | None,
    semantic_mappings: Path,
    semantic_vocabulary: Path,
    semantic_ids_lock: Path,
    competency_suite: Path | None,
    runtime_config_template: Path | None,
    embed: bool,
    skip_search: bool,
    deploy_serving: bool,
    deploy_knowledge: bool,
    data_agent_mode: str | None,
    data_agent_id: str | None,
    data_agent_name: str | None,
    approve_data_agent_replace: bool,
    deploy_agent: bool,
    deploy_app: bool,
    graph_preview_acknowledged: bool,
    approve_breaking_semantic_migration: bool,
    previous_semantic_contract: Path | None,
    initialize_semantic_baseline: bool,
    dry_run: bool,
    resume: bool,
    force: bool,
    deploy_manifest_path: str | None,
) -> None:
    """Build, provision, deploy, and validate the complete knowledge platform."""
    if resume and force:
        raise BuildDeployError("--resume and --force are mutually exclusive.")
    if skip_search and (deploy_serving or deploy_knowledge or deploy_agent):
        raise BuildDeployError(
            "--skip-search cannot be combined with serving, knowledge, or agent deployment."
        )
    if deploy_agent and not deploy_knowledge:
        raise BuildDeployError("--deploy-agent requires --deploy-knowledge.")
    if deploy_knowledge and data_agent_mode is None:
        raise BuildDeployError(
            "--deploy-knowledge requires explicit --data-agent-mode "
            "update, create, or replace."
        )
    if approve_data_agent_replace and data_agent_mode != "replace":
        raise BuildDeployError(
            "--approve-data-agent-replace requires --data-agent-mode replace."
        )
    if data_agent_mode == "replace" and not approve_data_agent_replace:
        raise BuildDeployError(
            "--data-agent-mode replace requires "
            "--approve-data-agent-replace."
        )
    if deploy_app and not deploy_serving:
        raise BuildDeployError("--deploy-app requires --deploy-serving.")
    if runtime_config_template and not competency_suite:
        raise BuildDeployError(
            "--runtime-config requires --competency-suite."
        )
    if runtime_config_template and (
        not deploy_serving or not deploy_knowledge
    ):
        raise BuildDeployError(
            "--runtime-config requires --deploy-serving and "
            "--deploy-knowledge."
        )
    if deploy_serving and not dry_run and not graph_preview_acknowledged:
        raise BuildDeployError(
            "Live serving deployment requires --graph-preview-acknowledged."
        )
    if previous_semantic_contract and initialize_semantic_baseline:
        raise BuildDeployError(
            "--previous-semantic-contract and "
            "--initialize-semantic-baseline are mutually exclusive."
        )

    effective_run_id = run_id or str(uuid.uuid4())
    try:
        run_uuid = uuid.UUID(effective_run_id)
    except ValueError as exc:
        raise BuildDeployError("--run-id must be a valid UUID.") from exc
    effective_run_id = str(run_uuid)
    run_token = run_uuid.hex[:8]
    state_base = Path(state_dir) if state_dir else Path("build") / "runs"
    run_root = (
        state_base
        if state_base.name.lower() == effective_run_id.lower()
        else state_base / effective_run_id
    )
    paths = _runtime_paths(run_root)
    state_path = run_root / "state.json"
    if state_path.exists() and not (resume or force):
        raise BuildDeployError(
            f"Run state already exists at {state_path}. Use --resume or --force."
        )
    if force:
        for key in ("build", "dist", "release"):
            target = paths[key]
            if target.exists():
                shutil.rmtree(target)
        paths["registry"].unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)

    run_root.mkdir(parents=True, exist_ok=True)
    _prepare_metadata(
        Path(".foundry") / "agent-metadata.yaml",
        paths["metadata"],
        run_token,
    )
    state = _RunState(
        state_path,
        run_id=effective_run_id,
        environment=env,
        resume=resume,
    )
    config_path = Path(
        str((ctx.obj or {}).get("config", "fabric-kg.yaml"))
    ).resolve()
    domain_path = Path(domain_contract).resolve()
    # --semantic-contract is optional; required for non-dry-run builds.
    if semantic_contract is None and not dry_run:
        raise BuildDeployError(
            "--semantic-contract is required for live builds. "
            "Provide the approved canonical semantic contract YAML."
        )
    semantic_contract_path = semantic_contract.resolve() if semantic_contract else None
    semantic_mappings_path = semantic_mappings.resolve()
    semantic_vocabulary_path = semantic_vocabulary.resolve()
    semantic_ids_lock_path = semantic_ids_lock.resolve()
    local_semantic_baseline_path = (
        Path("build") / "semantic-state" / env / "contract.yaml"
    )
    comparison_semantic_baseline_path = (
        previous_semantic_contract.resolve()
        if previous_semantic_contract
        else local_semantic_baseline_path
    )
    source_path = Path(input_path).resolve()
    infra_root = Path(infra_dir).resolve()

    # Emit name resolution plan early (before loading infra manifest) so that
    # --dry-run --manifest always shows resolved names even when infra files
    # are not yet provisioned.
    if dry_run:
        try:
            from fabric_kg_builder.deploy.manifest import (  # noqa: PLC0415
                load_deployment_manifest,
            )
            from fabric_kg_builder.deploy.name_authority import (  # noqa: PLC0415
                manifest_from_env_config,
                render_name_resolution,
                resolve_item_name,
            )
            if deploy_manifest_path:
                _dry_bd_manifest = load_deployment_manifest(deploy_manifest_path)
            else:
                from fabric_kg_builder.cli.deploy_cmd import (  # type: ignore[attr-defined]  # noqa: PLC0415
                    _read_fabric_env_config,
                )
                _dry_fabric_cfg = _read_fabric_env_config(env, allow_placeholders=True)
                _dry_bd_manifest = manifest_from_env_config(_dry_fabric_cfg)
            click.echo("\n[build-deploy] Name resolution plan:")
            for _item_type in ("Ontology", "Lakehouse", "GraphModel", "DataAgent"):
                try:
                    _resolved = resolve_item_name(_dry_bd_manifest, _item_type)
                    click.echo(render_name_resolution(_resolved))
                except Exception:
                    pass
        except Exception:
            pass

    infra_manifest_path = infra_root / "environments" / f"{env}.yaml"

    from fabric_kg_builder.domain import (
        load_domain_contract,
        require_ready_domain_contract,
    )
    from fabric_kg_builder.infra.manifest import load_manifest

    domain = load_domain_contract(domain_path)
    manifest = load_manifest(infra_manifest_path)

    click.echo(f"[build-deploy] run_id      : {effective_run_id}")
    click.echo(f"[build-deploy] environment : {env}")
    click.echo(f"[build-deploy] state        : {run_root}")
    if deploy_knowledge:
        runtime_config_path = (
            Path("ontology") / "environments" / f"{env}.json"
        )
        runtime_fabric = {}
        if runtime_config_path.exists():
            runtime_payload = json.loads(
                runtime_config_path.read_text(encoding="utf-8")
            )
            if isinstance(runtime_payload, dict):
                runtime_fabric = runtime_payload.get("fabric", {})
        planned_agent_id = str(
            data_agent_id
            or (
                runtime_fabric.get("data_agent_item_id")
                if isinstance(runtime_fabric, dict)
                else ""
            )
            or ""
        )
        planned_agent_name = str(
            data_agent_name
            or (
                runtime_fabric.get("data_agent_display_name")
                if isinstance(runtime_fabric, dict)
                else ""
            )
            or f"fkg-{env}-data-agent"
        )
        click.echo(
            "[build-deploy] Data Agent plan: "
            f"workspace="
            f"{manifest.fabric.workspace.display_name or manifest.fabric.workspace.name or env}, "
            f"agent={planned_agent_name} "
            f"({planned_agent_id or 'new item'}), "
            f"actions={data_agent_mode}+publish"
        )

    state.execute(
        "domain_gate",
        lambda: {
            "contract": str(domain_path),
            "domain": domain.domain.name,
            "validated": bool(
                require_ready_domain_contract(str(domain_path))[2]
                .ready_for_enrichment
            ),
        },
        resume=resume,
    )
    if dry_run:
        if provision:
            state.execute(
                "infrastructure_plan",
                lambda: _invoke_cli(
                    [
                        "infra",
                        "apply",
                        "--env",
                        env,
                        "--infra-dir",
                        str(infra_root),
                        "--build-root",
                        str(run_root),
                        "--auto-approve",
                        "--dry-run",
                    ],
                    config_path=config_path,
                    environment=env,
                ),
                resume=resume,
            )
        state.data["planned_stages"] = [
            "inspect_source",
            "enrich",
            *(["densify"] if densify_config else []),
            "compile_data",
            "compile_semantic",
            "compile_ontology",
            "compile_graph",
            "compile_agent",
            *(["compile_search"] if not skip_search else []),
            "package",
            "validate",
            *(
                [
                    "deploy_lakehouse",
                    "deploy_ontology",
                    "deploy_serving",
                    "validate_projection",
                ]
                if deploy_serving
                else []
            ),
            *(["deploy_knowledge"] if deploy_knowledge else []),
            *(["deploy_agent"] if deploy_agent else []),
            *(["deploy_app"] if deploy_app else []),
            "deployment_receipt",
            *(["runtime_config", "runtime_acceptance"] if runtime_config_template else []),
        ]
        state.complete(dry_run=True)
        # Name plan was already emitted at function start; just signal completion.
        click.echo("[build-deploy] DRY RUN complete; no build or deployment mutations ran.")
        return

    if deploy_serving:
        state.execute(
            "semantic_compatibility",
            lambda: _semantic_compatibility_gate(
                current_contract_path=semantic_contract_path,
                baseline_contract_path=comparison_semantic_baseline_path,
                report_path=(
                    paths["release"] / "semantic-compatibility.json"
                ),
                approve_breaking_migration=(
                    approve_breaking_semantic_migration
                ),
                initialize_baseline=initialize_semantic_baseline,
                enforce=True,
            ),
            resume=resume,
        )

    state.execute(
        "infrastructure_preflight",
        lambda: _invoke_cli(
            [
                "infra",
                "preflight",
                "--env",
                env,
                "--infra-dir",
                str(infra_root),
                "--json",
            ],
            config_path=config_path,
            environment=env,
        ),
        resume=resume,
    )

    if provision:
        state.execute(
            "infrastructure",
            lambda: _invoke_cli(
                [
                    "infra",
                    "apply",
                    "--env",
                    env,
                    "--infra-dir",
                    str(infra_root),
                    "--build-root",
                    str(run_root),
                    "--auto-approve",
                ],
                config_path=config_path,
                environment=env,
            ),
            resume=resume,
        )
    else:
        state.execute(
            "infrastructure_import",
            lambda: _import_infrastructure_state(
                environment=env,
                run_root=run_root,
                explicit_outputs_path=(
                    infra_outputs.resolve() if infra_outputs else None
                ),
            ),
            resume=resume,
        )

    from fabric_kg_builder.infra.apply import load_outputs
    from fabric_kg_builder.infra.runtime_sync import sync_runtime_configuration

    outputs = load_outputs(run_root, env)
    if not outputs:
        raise BuildDeployError(
            f"Infrastructure outputs are missing under {run_root / 'infra' / env}."
        )
    sync_runtime_configuration(
        environment=env,
        manifest=manifest,
        outputs=outputs,
        fabric_environment_path=(
            Path("ontology") / "environments" / f"{env}.json"
        ),
        agent_metadata_path=paths["metadata"],
    )

    runtime_env = _runtime_environment(
        outputs=outputs,
        run_root=run_root,
        environment=env,
    )

    state.execute(
        "inspect_source",
        lambda: _invoke_cli(
            [
                "inspect-source",
                "--input",
                str(source_path),
                "--format",
                "json",
                "--out",
                str(paths["inspection"]),
            ],
            config_path=config_path,
            environment=env,
            extra_env=runtime_env,
        ),
        resume=resume,
    )

    enrich_args = [
        "enrich",
        "--input",
        str(source_path),
        "--registry",
        str(paths["registry"]),
        "--run-id",
        effective_run_id,
        "--domain-file",
        str(domain_path),
        "--semantic-contract",
        str(semantic_contract_path),
        "--semantic-mappings",
        str(semantic_mappings_path),
        "--semantic-vocabulary",
        str(semantic_vocabulary_path),
        "--semantic-ids-lock",
        str(semantic_ids_lock_path),
        "--require-semantic-contract",
        "--out",
        str(paths["enriched"]),
        "--drawing-mode",
        drawing_mode,
    ]
    if recursive:
        enrich_args.append("--recursive")
    if resume:
        enrich_args.append("--resume")
    if force:
        enrich_args.append("--force")
    state.execute(
        "enrich",
        lambda: _invoke_cli(
            enrich_args,
            config_path=config_path,
            environment=env,
            extra_env=runtime_env,
        ),
        resume=resume,
    )

    compile_input = paths["enriched"]
    if densify_config:
        state.execute(
            "densify",
            lambda: _invoke_cli(
                [
                    "densify",
                    "--input",
                    str(paths["enriched"]),
                    "--out",
                    str(paths["enriched_dense"]),
                    "--domain-file",
                    str(domain_path),
                    "--densify-config",
                    str(Path(densify_config).resolve()),
                    "--link-associations",
                    "--link-steps",
                    "--link-diagnostics",
                ],
                config_path=config_path,
                environment=env,
                extra_env=runtime_env,
            ),
            resume=resume,
        )
        compile_input = paths["enriched_dense"]

    state.execute(
        "compile_data",
        lambda: _invoke_cli(
            [
                "compile-data",
                "--input",
                str(compile_input),
                "--out",
                str(paths["parquet"]),
                "--validate",
            ],
            config_path=config_path,
            environment=env,
            extra_env=runtime_env,
        ),
        resume=resume,
    )
    semantic_args = [
        "--contract",
        str(semantic_contract_path),
        "--mappings",
        str(semantic_mappings_path),
        "--vocabulary",
        str(semantic_vocabulary_path),
        "--ids-lock",
        str(semantic_ids_lock_path),
    ]
    semantic_compile_args = [
        *semantic_args,
        "--data-version",
        effective_run_id,
        "--data-dir",
        str(paths["parquet"]),
    ]
    semantic_quality_report = next(
        (
            candidate
            for candidate in (
                Path(compile_input) / "semantic-quality-report.json",
                paths["enriched"] / "semantic-quality-report.json",
            )
            if candidate.exists()
        ),
        None,
    )
    if semantic_quality_report is not None:
        semantic_compile_args.extend(
            ["--quality-report", str(semantic_quality_report)]
        )
    state.execute(
        "compile_semantic",
        lambda: _invoke_cli(
            [
                "compile-semantic",
                *semantic_compile_args,
                "--out",
                str(paths["semantic"]),
            ],
            config_path=config_path,
            environment=env,
            extra_env=runtime_env,
        ),
        resume=resume,
    )
    state.execute(
        "compile_ontology",
        lambda: _invoke_cli(
            [
                "compile-ontology",
                "--semantic-dir",
                str(paths["semantic"]),
                "--out",
                str(paths["ontology"]),
                "--env",
                env,
            ],
            config_path=config_path,
            environment=env,
            extra_env=runtime_env,
        ),
        resume=resume,
    )
    state.execute(
        "compile_graph",
        lambda: _invoke_cli(
            [
                "compile-graph",
                "--semantic-dir",
                str(paths["semantic"]),
                "--out",
                str(paths["graph"]),
                "--workspace-id",
                str(outputs.get("fabricWorkspaceId") or ""),
                "--lakehouse-id",
                str(outputs.get("fabricLakehouseId") or ""),
            ],
            config_path=config_path,
            environment=env,
            extra_env=runtime_env,
        ),
        resume=resume,
    )
    agent_args = [
        "compile-agent",
        "--semantic-dir",
        str(paths["semantic"]),
        "--out",
        str(paths["agents"]),
        "--domain-context",
        _domain_instruction(domain),
    ]
    for question in domain.competency_questions:
        agent_args.extend(["--question", str(question)])
    if competency_suite:
        agent_args.extend(
            ["--competency-suite", str(competency_suite.resolve())]
        )
    state.execute(
        "compile_agent",
        lambda: _invoke_cli(
            agent_args,
            config_path=config_path,
            environment=env,
            extra_env=runtime_env,
        ),
        resume=resume,
    )

    if not skip_search:
        search_args = [
            "compile-search",
            "--input",
            str(paths["parquet"]),
            "--out",
            str(paths["search"]),
            "--embedding-deployment",
            str(outputs.get("embeddingDeploymentName") or "embedding"),
            "--semantic-manifest",
            str(paths["semantic"] / "semantic-manifest.json"),
            "--semantic-model-manifest",
            str(paths["semantic"] / "semantic-model-manifest.json"),
            "--semantic-crosswalk",
            str(paths["semantic"] / "semantic-crosswalk.json"),
            "--require-semantic-contract",
        ]
        if embed:
            search_args.append("--embed")
        state.execute(
            "compile_search",
            lambda: _invoke_cli(
                search_args,
                config_path=config_path,
                environment=env,
                extra_env=runtime_env,
            ),
            resume=resume,
        )

    package_args = [
        "package",
        "--build-dir",
        str(paths["build"]),
        "--out",
        str(paths["dist"]),
    ]
    if not skip_search:
        package_args.append("--include-search")
    state.execute(
        "package",
        lambda: _invoke_cli(
            package_args,
            config_path=config_path,
            environment=env,
            extra_env=runtime_env,
        ),
        resume=resume,
    )
    state.execute(
        "validate",
        lambda: _invoke_cli(
            [
                "validate",
                "--env",
                env,
                "--build",
                str(paths["build"]),
                "--report",
                str(paths["release"] / "validation.json"),
            ],
            config_path=config_path,
            environment=env,
            extra_env=runtime_env,
        ),
        resume=resume,
    )

    search_index_name = f"fkg-{run_token}-chunks"
    if deploy_serving:
        schema_file = paths["search"] / "kg-chunks" / "index.schema.json"
        docs_file = paths["search"] / "kg-chunks" / "docs.json"
        state.execute(
            "deploy_lakehouse",
            lambda: _invoke_cli(
                [
                    "deploy-lakehouse",
                    "--env",
                    env,
                    "--parquet-dir",
                    str(paths["parquet"]),
                    "--no-mock",
                    *(["--manifest", deploy_manifest_path] if deploy_manifest_path else []),
                ],
                config_path=config_path,
                environment=env,
                extra_env=runtime_env,
            ),
            resume=resume,
        )
        state.execute(
            "deploy_ontology",
            lambda: _invoke_cli(
                [
                    "deploy-ontology",
                    "--env",
                    env,
                    "--dist",
                    str(paths["ontology"]),
                    "--semantic-dir",
                    str(paths["semantic"]),
                    "--parquet-dir",
                    str(paths["parquet"]),
                    "--no-mock",
                    "--receipt-out",
                    str(paths["release"] / "ontology-deployment.json"),
                    *(["--manifest", deploy_manifest_path] if deploy_manifest_path else []),
                ],
                config_path=config_path,
                environment=env,
                extra_env=runtime_env,
            ),
            resume=resume,
        )
        state.execute(
            "record_semantic_baseline",
            lambda: _record_semantic_baseline(
                semantic_contract_path,
                local_semantic_baseline_path,
            ),
            resume=resume,
        )
        state.execute(
            "deploy_serving",
            lambda: _invoke_cli(
                [
                    "deploy-serving",
                    "--env",
                    env,
                    "--index-name",
                    search_index_name,
                    "--schema-file",
                    str(schema_file),
                    "--docs-file",
                    str(docs_file),
                    "--embedding-model",
                    "text-embedding-3-large",
                    "--dimensions",
                    "1536",
                    "--run-id",
                    effective_run_id,
                    "--no-dry-run",
                    "--dist",
                    str(paths["dist"]),
                    "--graph-artifact-out",
                    str(paths["graph"]),
                    "--graph-definition-file",
                    str(paths["graph"] / "graph-definition.json"),
                    "--semantic-dir",
                    str(paths["semantic"]),
                    "--label-catalog-file",
                    str(paths["graph"] / "label-catalog.json"),
                    "--skip-lakehouse",
                    "--graph-preview-acknowledged",
                    "--receipt-out",
                    str(paths["release"] / "serving-deployment.json"),
                    *(["--manifest", deploy_manifest_path] if deploy_manifest_path else []),
                ],
                config_path=config_path,
                environment=env,
                extra_env=runtime_env,
            ),
            resume=resume,
        )
        state.execute(
            "validate_projection",
            lambda: _invoke_cli(
                [
                    "validate-projection",
                    "--semantic-dir",
                    str(paths["semantic"]),
                    "--ontology-receipt",
                    str(paths["release"] / "ontology-deployment.json"),
                    "--serving-receipt",
                    str(paths["release"] / "serving-deployment.json"),
                    "--out",
                    str(
                        paths["release"]
                        / "persisted-projection-receipt.json"
                    ),
                ],
                config_path=config_path,
                environment=env,
                extra_env=runtime_env,
            ),
            resume=resume,
        )

    if deploy_knowledge:
        knowledge_index_name = _knowledge_search_index_name(
            paths["release"] / "serving-deployment.json",
            search_index_name,
        )
        state.execute(
            "deploy_knowledge",
            lambda: _deploy_knowledge(
                environment=env,
                run_token=run_token,
                domain_contract=domain,
                metadata_path=paths["metadata"],
                outputs=outputs,
                manifest=manifest,
                search_index_name=knowledge_index_name,
                semantic_dir=paths["semantic"],
                persisted_projection_path=(
                    paths["release"]
                    / "persisted-projection-receipt.json"
                ),
                semantic_context_path=(
                    paths["agents"] / "semantic-context.json"
                ),
                agent_instructions_path=paths["agents"] / "instructions.md",
                agent_publication_receipt_path=(
                    paths["release"]
                    / "agent-publication-receipt.json"
                ),
                workspace_name=str(
                    manifest.fabric.workspace.display_name
                    or manifest.fabric.workspace.name
                    or env
                ),
                data_agent_mode=str(data_agent_mode),
                data_agent_item_id=data_agent_id,
                data_agent_display_name=data_agent_name,
                approve_data_agent_replace=(
                    approve_data_agent_replace
                ),
                require_foundry_search_connection=deploy_agent,
                deploy_manifest_path=deploy_manifest_path,
            ),
            resume=resume,
        )

    if deploy_agent:
        state.execute(
            "deploy_agent",
            lambda: (
                _invoke_cli(
                    [
                        "app",
                        "deploy-agent",
                        "--env",
                        env,
                        "--metadata",
                        str(paths["metadata"]),
                        "--registry",
                        str(paths["registry"]),
                    ],
                    config_path=config_path,
                    environment=env,
                    extra_env=runtime_env,
                )
                | {
                    "agent_name": str(
                        (
                            yaml.safe_load(
                                paths["metadata"].read_text(encoding="utf-8")
                            )
                            or {}
                        ).get("agentName", "")
                    ),
                    "agent_version_id": str(
                        (
                            (
                                yaml.safe_load(
                                    paths["metadata"].read_text(encoding="utf-8")
                                )
                                or {}
                            )
                            .get("deploymentContext", {})
                            .get(env, {})
                            .get("agent_version_id", "")
                        )
                    ),
                }
            ),
            resume=resume,
        )

    if deploy_app:
        state.execute(
            "fabric_runtime_access",
            lambda: _ensure_fabric_runtime_access(
                workspace_id=str(outputs.get("fabricWorkspaceId") or ""),
                principal_id=str(outputs.get("identityPrincipalId") or ""),
            ),
            resume=resume,
        )
        app_env = {
            **runtime_env,
            "FABRIC_KG_TENANT_ID": os.environ.get(
                "FABRIC_KG_TENANT_ID",
                os.environ.get("AZURE_TENANT_ID", ""),
            ),
            "FABRIC_KG_AUDIENCE": os.environ.get("FABRIC_KG_AUDIENCE", ""),
            "FABRIC_KG_GRAPH_PREVIEW_ACKNOWLEDGED": "true",
            "FABRIC_KG_DOWNSTREAM_ACCESS_CONFIRMED": "true",
        }
        state.execute(
            "deploy_app",
            lambda: _invoke_cli(
                [
                    "app",
                    "deploy-app",
                    "--env",
                    env,
                    "--run-id",
                    effective_run_id,
                    "--metadata",
                    str(paths["metadata"]),
                    "--registry",
                    str(paths["registry"]),
                    "--out",
                    str(paths["release"] / "app-deployment.json"),
                ],
                config_path=config_path,
                environment=env,
                extra_env=app_env,
            ),
            resume=resume,
        )

    state.execute(
        "deployment_receipt",
        lambda: _write_semantic_deployment_receipt(
            run_id=effective_run_id,
            environment=env,
            paths=paths,
            state=state,
        ),
        resume=resume,
    )
    ledger = _build_resource_ledger(
        run_id=effective_run_id,
        environment=env,
        run_root=run_root,
        outputs=outputs,
        manifest=manifest,
        state=state,
        search_index_name=search_index_name,
    )
    state.data["resource_ledger"] = str(paths["release"] / "ledger.json")
    state.data["resource_count"] = len(ledger["resources"])
    if runtime_config_template:
        runtime_config_path = paths["release"] / "runtime-config.json"
        state.execute(
            "runtime_config",
            lambda: _prepare_runtime_acceptance_config(
                template_path=runtime_config_template.resolve(),
                target_path=runtime_config_path,
                receipt_path=paths["release"] / "deployment-receipt.json",
                environment=env,
                outputs=outputs,
                state=state,
            ),
            resume=resume,
        )
        state.execute(
            "runtime_acceptance",
            lambda: _run_runtime_acceptance(
                competency_contract_path=(
                    paths["agents"] / "competency-contract.json"
                ),
                runtime_config_path=runtime_config_path,
                release_dir=paths["release"],
            ),
            resume=resume,
        )
    state.complete()
    click.echo(
        f"\n[build-deploy] SUCCESS run={effective_run_id} "
        f"state={run_root}"
    )
