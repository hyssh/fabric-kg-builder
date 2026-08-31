"""cli/app_cmd.py — M8 app management CLI commands.

Commands (registered in main.py):
  deploy-agent   Deploy the Foundry prompt-agent from agent-metadata.yaml.
  deploy-app     Build + ACR push + Bicep deploy Container Apps; rollback on
                 unhealthy revision; record lineage only after remote success.
  test           Route offline cases through actual routing; persist evidence
                 under .foundry/results/<env>/; live cases via deployed agent.

Design:
  - External I/O is injectable via runner/client parameters for testing.
  - Deployment lineage is recorded ONLY after confirmed remote success.
  - No secrets accepted on the CLI; all from env vars or agent-metadata.yaml.
  - dry-run NEVER persists deploymentContext or records success lineage.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import click

from fabric_kg_builder.agent.deployer import deploy_agent, DeploymentContext, DeploymentError
from fabric_kg_builder.agent.metadata import load_agent_metadata, AgentMetadataError
from fabric_kg_builder.agent.evaluator import (
    load_eval_dataset,
    run_evaluation,
    EvalCase,
)
from fabric_kg_builder.agent.l7_release import (
    AzureL7Backend,
    L7Executor,
    L7Planner,
    L7ReleaseError,
    ObservationBackend,
    load_l7_config,
    load_observation,
    load_plan,
    persist_plan,
)
from fabric_kg_builder.lineage.registry import AssetRegistry, record_deployment

_DEFAULT_REGISTRY_PATH = Path("build") / "lineage" / "registry.json"
_DEFAULT_METADATA_PATH = Path(".foundry") / "agent-metadata.yaml"
_RESULTS_ROOT = Path(".foundry") / "results"


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _default_runner(cmd: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    """Default subprocess runner — used in production."""
    executable = cmd[0]
    if os.name == "nt" and executable == "az":
        executable = "az.cmd"
    return subprocess.run([executable, *cmd[1:]], capture_output=True, text=True, **kwargs)


def _load_infra_outputs(environment: str) -> dict[str, Any]:
    configured = os.environ.get("FABRIC_KG_INFRA_OUTPUTS_PATH", "")
    path = (
        Path(configured)
        if configured
        else Path("build") / "infra" / environment / "outputs.json"
    )
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Unable to read infrastructure outputs at {path}: {exc}")
    return payload if isinstance(payload, dict) else {}


def _load_serving_environment(environment: str) -> dict[str, Any]:
    """Load non-secret search and Blob bindings for an app environment."""
    path = Path("ontology") / "environments" / f"{environment}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Unable to read serving environment at {path}: {exc}")
    return payload if isinstance(payload, dict) else {}


def _env_or_output(
    environment_name: str,
    outputs: dict[str, Any],
    output_name: str,
    default: str = "",
) -> str:
    value = os.environ.get(environment_name)
    if value is not None and value.strip():
        return value.strip()
    output_value = outputs.get(output_name, default)
    return str(output_value or default).strip()


def _is_true_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def _current_azure_principal_id(runner: Callable) -> str:
    """Best-effort resolution of the currently signed-in Azure principal."""
    account = runner(
        ["az", "account", "show", "--query", "user", "--output", "json"]
    )
    if account.returncode != 0:
        return ""
    try:
        user = json.loads(account.stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    principal_type = str(user.get("type") or "").lower()
    principal_name = str(user.get("name") or "")
    if principal_type == "serviceprincipal" and principal_name:
        command = [
            "az", "ad", "sp", "show",
            "--id", principal_name,
            "--query", "id",
            "--output", "tsv",
        ]
    else:
        command = [
            "az", "ad", "signed-in-user", "show",
            "--query", "id",
            "--output", "tsv",
        ]
    resolved = runner(command)
    return resolved.stdout.strip() if resolved.returncode == 0 else ""


# ---------------------------------------------------------------------------
# Lineage helpers
# ---------------------------------------------------------------------------


def _record_agent_lineage(
    registry_path: Path,
    environment: str,
    ctx: DeploymentContext,
) -> None:
    """Persist agent deployment to lineage registry (only called after live success)."""
    try:
        registry = AssetRegistry(store_path=registry_path, environment=environment)
        store = registry.load()
        record_deployment(
            store.setdefault("deployments", []),
            run_id="agent-deploy-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S"),
            environment=environment,
            artifact_type="prompt-agent",
            artifact_version=ctx.agent_version or ctx.instructions_version,
            target_resource_id=ctx.agent_version_id or None,
            target_name=ctx.agent_name,
            target_record_locator=None,
            status="succeeded",
            operation_id=ctx.agent_version_id or None,
        )
        registry.save(store)
    except Exception as exc:
        click.echo(f"  [warn] lineage registry update failed: {exc}", err=True)


def _record_app_lineage(
    registry_path: Path,
    environment: str,
    tag: str,
    components: list[tuple[str, str, str | None]],
    *,
    run_id: str | None = None,
) -> None:
    """Persist container deployments to lineage (only called after Bicep succeeds)."""
    try:
        registry = AssetRegistry(store_path=registry_path, environment=environment)
        store = registry.load()
        for name, img, resource_id in components:
            record_deployment(
                store.setdefault("deployments", []),
                run_id=run_id or ("app-deploy-" + tag),
                environment=environment,
                artifact_type=f"container/{name}",
                artifact_version=tag,
                target_resource_id=resource_id,
                target_name=img,
                target_record_locator=None,
                status="succeeded",
            )
        registry.save(store)
    except Exception as exc:
        click.echo(f"  [warn] lineage registry update failed: {exc}", err=True)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group(
    "app",
    context_settings={"max_content_width": 120, "help_option_names": ["-h", "--help"]},
)
def app_cmd() -> None:
    """M8 agent and app deployment commands."""


@app_cmd.command("compile-l6")
@click.option("--agent-name", required=True)
@click.option("--fabric-connection-id", required=True)
@click.option(
    "--out",
    "output_path",
    default="build/release/l6-agent-definition.json",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
def compile_l6_cmd(
    agent_name: str,
    fabric_connection_id: str,
    output_path: Path,
) -> None:
    """Compile and persist the canonical local L6 five-tool definition."""
    from fabric_kg_builder.agent.l6_integration import (
        build_l6_agent_definition,
        persist_l6_agent_definition,
    )

    try:
        definition = build_l6_agent_definition(
            agent_name=agent_name,
            fabric_data_agent_connection_id=fabric_connection_id,
        )
        definition_hash = persist_l6_agent_definition(
            output_path,
            definition,
            expected_definition=definition,
        )
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"definition_path={output_path}")
    click.echo(f"definition_hash={definition_hash}")


@app_cmd.command("deploy-l7")
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Strict non-secret 0.2.4 L7 JSON configuration.",
)
@click.option(
    "--dry-run/--live",
    default=True,
    show_default=True,
    help="Generate a GET-only plan by default; live requires exact approval.",
)
@click.option(
    "--plan",
    "plan_path",
    default="build/release/l7-0.2.4-plan.json",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
@click.option(
    "--observation",
    "observation_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Dry-run-only local observation fixture for black-box smoke tests.",
)
@click.option("--resume", is_flag=True, help="Reuse only the exact persisted plan.")
@click.option(
    "--approve-live",
    default=None,
    help=(
        "Optional exact persisted plan hash. Without it, --live performs and "
        "persists a fresh preflight plan, then approves that exact hash internally."
    ),
)
@click.option(
    "--out",
    "receipt_path",
    default="build/release/l7-0.2.4-receipt.json",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
@click.option(
    "--log",
    "log_path",
    default="build/release/l7-0.2.4-events.jsonl",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Sanitized structured operation event log.",
)
def deploy_l7_cmd(
    config_path: Path,
    dry_run: bool,
    plan_path: Path,
    observation_path: Path | None,
    resume: bool,
    approve_live: str | None,
    receipt_path: Path,
    log_path: Path,
) -> None:
    """Plan or execute the narrowed 0.2.4 L7 release transaction."""
    def emit(event: dict[str, Any], *, required: bool = True) -> None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            if required:
                raise L7ReleaseError(
                    "sanitized operation log is not durably writable"
                ) from exc

    entered_execution = False
    try:
        config = load_l7_config(config_path)
        if observation_path is not None and not dry_run:
            raise L7ReleaseError("--observation is forbidden in live mode")
        backend = (
            ObservationBackend(load_observation(observation_path))
            if observation_path is not None
            else AzureL7Backend(config_path.parent)
        )
        planner = L7Planner(backend)
        if dry_run:
            if approve_live:
                raise L7ReleaseError("--approve-live is invalid in dry-run mode")
            if resume:
                persisted = load_plan(plan_path)
                fresh = planner.build(
                    config,
                    config_path=config_path,
                    attempt_id=persisted.attempt_id,
                )
                if (
                    persisted.config_hash != fresh.config_hash
                    or persisted.tenant_id != fresh.tenant_id
                    or persisted.principal_hash != fresh.principal_hash
                    or persisted.observation_hash != fresh.observation_hash
                    or persisted.actions != fresh.actions
                ):
                    raise L7ReleaseError(
                        "--resume requires exact current config, identity, and readback"
                    )
                plan = persisted
            else:
                plan = planner.build(config, config_path=config_path)
                persist_plan(plan_path, plan)
            click.echo(f"plan_hash={plan.plan_hash}")
            click.echo(f"plan_path={plan_path}")
            click.echo("mode=dry-run; mutations=0")
            click.echo("l6_hosting=generated-local-deferred")
            blockers = [item.component for item in plan.actions if item.action == "no-go"]
            if blockers:
                click.echo(f"capability_no_go={','.join(blockers)}")
            emit(
                {
                    "event": "plan",
                    "mode": "dry-run",
                    "plan_hash": plan.plan_hash,
                    "expires_at": plan.expires_at.isoformat(),
                    "actions": [
                        {
                            "component": item.component,
                            "action": item.action,
                            "resource_id": item.resource_id,
                            "desired_hash": item.desired_hash,
                            "etag": item.observed_etag,
                        }
                        for item in plan.actions
                    ],
                }
            )
            return
        if resume:
            raise L7ReleaseError(
                "--resume is a dry-run readback check; live always consumes --plan"
            )
        if approve_live:
            plan = load_plan(plan_path)
            approval = approve_live
        else:
            plan = planner.build(config, config_path=config_path)
            persist_plan(plan_path, plan)
            approval = plan.plan_hash
        emit(
            {
                "event": "preflight-approved",
                "mode": "live",
                "plan_hash": plan.plan_hash,
                "expires_at": plan.expires_at.isoformat(),
                "mutation_count": len(
                    [
                        item
                        for item in plan.actions
                        if item.action in {"create", "update"}
                    ]
                ),
            }
        )
        entered_execution = True
        receipt = L7Executor(planner, backend).execute(
            config=config,
            config_path=config_path,
            plan=plan,
            approval=approval,
            receipt_path=receipt_path,
        )
        click.echo(f"status={receipt.status}")
        click.echo(f"receipt_hash={receipt.receipt_hash}")
        click.echo(f"receipt_path={receipt_path}")
        for entry in receipt.journal:
            emit(
                {"event": "journal", **entry.model_dump(mode="json")},
                required=False,
            )
        emit(
            {
                "event": "complete",
                "status": receipt.status,
                "receipt_hash": receipt.receipt_hash,
                "plan_hash": receipt.plan_hash,
            },
            required=False,
        )
    except L7ReleaseError as exc:
        failure_receipt_path = receipt_path.with_name(
            f"{receipt_path.name}.failure.json"
        )
        failure_event: dict[str, object] = {
            "event": "failure",
            "error_type": type(exc).__name__,
            "causal_stage": "execution" if entered_execution else "preflight",
            "mutation_possible": entered_execution,
            "message": str(exc),
        }
        if entered_execution:
            for key, candidate in (
                ("receipt_path", receipt_path),
                ("failure_receipt_path", failure_receipt_path),
            ):
                if candidate.exists():
                    failure_event[key] = str(candidate)
        emit(failure_event, required=False)
        raise click.ClickException(str(exc)) from exc


# ---------------------------------------------------------------------------
# deploy-agent
# ---------------------------------------------------------------------------


_DEPLOY_AGENT_EPILOG = """\b
Live mode reads .foundry/agent-metadata.yaml, connects to the Foundry project
via DefaultAzureCredential, validates schema, creates/updates the agent,
verifies readiness, runs a smoke prompt, and persists deploymentContext.

Required order:
  validate-projection -> deploy-data-agent -> configure the new Fabric Data
  Agent connection in agent-metadata.yaml -> app deploy-agent

Do not deploy the Foundry agent first when it is expected to use a newly
published Fabric Data Agent. Verify that environments.<env>.connections.
fabricDataAgent points to the intended Data Agent connection.

Live dependency:
  azure-ai-projects>=2.3.0 and azure-identity must be installed in the
  fabric-kg CLI runtime. Dry-run validates metadata without these live calls.

PowerShell example:
\b
  fabric-kg app deploy-agent --env dev --dry-run
  fabric-kg app deploy-agent --env dev       # live; requires Azure credentials

Environment selection: explicit --env > metadata.defaultEnvironment
No secrets are accepted on the command line.
"""


@app_cmd.command("deploy-agent", epilog=_DEPLOY_AGENT_EPILOG)
@click.option("--env", default=None, type=click.Choice(["dev", "test", "prod"]),
              help="Target environment (default: metadata.defaultEnvironment).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Validate metadata and schema only. Never persists deploymentContext.")
@click.option("--metadata", "metadata_path", default=None, type=click.Path(),
              help="Override path to agent-metadata.yaml.")
@click.option("--registry", "registry_path", default=None, type=click.Path(),
              help="Override path to lineage registry.json.")
@click.option("--domain-contract", default=None, type=click.Path(exists=True),
              help="Approved domain.yaml context for the deployed prompt agent.")
@click.option("--entity-types-file", default=None, type=click.Path(exists=True),
              help="multitype-plan.json used to ground valid entity and relationship types.")
@click.pass_context
def deploy_agent_cmd(
    ctx: click.Context,
    env: str | None,
    dry_run: bool,
    metadata_path: str | None,
    registry_path: str | None,
    domain_contract: str | None,
    entity_types_file: str | None,
) -> None:
    """Deploy the Foundry prompt-agent from agent-metadata.yaml."""
    md_path = Path(metadata_path) if metadata_path else _DEFAULT_METADATA_PATH
    reg_path = Path(registry_path) if registry_path else _DEFAULT_REGISTRY_PATH
    verbose = ctx.obj.get("verbose", False) if ctx.obj else False

    try:
        metadata = load_agent_metadata(md_path)
        environment = env or metadata.defaultEnvironment
    except AgentMetadataError as exc:
        click.echo(f"Error loading agent metadata: {exc}", err=True)
        sys.exit(1)

    click.echo(
        f"deploy-agent: env={environment}, agent={metadata.agentName}"
        + (" [DRY RUN — no deployment, no context persistence]" if dry_run else "")
    )

    entity_types: list[str] | None = None
    relationship_types: list[str] | None = None
    if entity_types_file:
        plan = json.loads(Path(entity_types_file).read_text(encoding="utf-8"))
        entity_types = [
            str(item["type_name"])
            for item in plan.get("entity_types", [])
            if isinstance(item, dict) and item.get("type_name")
        ]
        relationship_types = [
            str(item["name"])
            for item in plan.get("relationship_pairs", [])
            if isinstance(item, dict) and item.get("name")
        ] or None
    domain_context: str | None = None
    if domain_contract:
        from fabric_kg_builder.domain import require_ready_domain_contract

        contract, _review, _status = require_ready_domain_contract(
            domain_contract
        )
        domain_context = (
            f"Domain: {contract.domain.name}. "
            f"Business context: {contract.business.organization_context}. "
            f"Problem: {contract.problem.statement}"
        )

    try:
        # _client=None → deployer builds FoundryAgentClient from metadata + DefaultAzureCredential
        # dry_run=True → validate + schema fetch only, NEVER persists
        deployment_ctx = deploy_agent(
            environment=environment,
            _client=None,
            metadata_path=md_path,
            entity_types=entity_types,
            relationship_types=relationship_types,
            domain_context=domain_context,
            dry_run=dry_run,
            require_grounding_tools=True,
        )
    except DeploymentError as exc:
        click.echo(f"Deployment failed: {exc}", err=True)
        sys.exit(1)

    click.echo(f"  model_deployment   : {deployment_ctx.model_deployment}")
    click.echo(f"  instructions_ver   : {deployment_ctx.instructions_version}")
    click.echo(f"  instructions_hash  : {deployment_ctx.instructions_hash}")
    click.echo(f"  schema_valid       : {deployment_ctx.schema_valid}")
    click.echo(f"  agent_ready        : {deployment_ctx.agent_ready}")
    click.echo(f"  smoke_passed       : {deployment_ctx.smoke_passed}")
    if deployment_ctx.agent_version_id:
        click.echo(f"  agent_version_id   : {deployment_ctx.agent_version_id}")
    if deployment_ctx.agent_version:
        click.echo(f"  agent_version      : {deployment_ctx.agent_version}")
    if verbose:
        click.echo(f"  image_tag          : {deployment_ctx.image_tag}")

    if not dry_run:
        # Only record lineage after live success (smoke_passed + agent_version_id set)
        if deployment_ctx.smoke_passed and deployment_ctx.agent_version_id:
            _record_agent_lineage(reg_path, environment, deployment_ctx)
            click.echo("  lineage recorded.")
        else:
            click.echo("  [warn] deployment incomplete — lineage NOT recorded.", err=True)
            sys.exit(1)
    else:
        click.echo("  dry-run complete. deploymentContext was NOT written.")


# ---------------------------------------------------------------------------
# deploy-app
# ---------------------------------------------------------------------------


_DEPLOY_APP_EPILOG = """\b
Full deployment lifecycle:
  1. Build linux/amd64 images with timestamp tag.
  2. ACR login (via az acr login --name <registry>) + push.
  3. Bicep deployment (az deployment group create).
  4. Poll Container Apps revision for healthy status.
  5. Rollback to previous revision if unhealthy.
  6. Record canonical lineage ONLY after confirmed health.

Example:
  fabric-kg app deploy-app --env dev --dry-run
  fabric-kg app deploy-app --env prod

Env secrets are NEVER baked into images or passed on the command line.
"""


@app_cmd.command("deploy-app", epilog=_DEPLOY_APP_EPILOG)
@click.option("--env", default="dev", type=str,
              help="Target environment.")
@click.option("--image-tag", default=None,
              help="Container image tag (default: UTC timestamp YYYYMMDDTHHMMSS).")
@click.option("--run-id", default=None,
              help="Owning build-deploy run UUID; scopes resource names and tags.")
@click.option("--out", "receipt_path", default=None, type=click.Path(),
              help="Write a non-secret deployment receipt JSON after success.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print deployment plan. Never records success lineage.")
@click.option("--registry", "registry_path", default=None, type=click.Path(),
              help="Override path to lineage registry.json.")
@click.option("--metadata", "metadata_path", default=None, type=click.Path(),
              help="Override path to agent-metadata.yaml.")
@click.option("--rollback-on-unhealthy/--no-rollback", default=True,
              help="Rollback to previous revision if new revision is unhealthy.")
@click.option("--cloud-build/--local-build", default=True,
              help="Build images with ACR Tasks (default) or local Docker.")
@click.option("--build-context", default=".", type=click.Path(exists=True, file_okay=False),
              help="Docker build context directory (default: repository root).")
@click.option("--skip-build", is_flag=True, default=False,
              help="Deploy already-published API and UI images without rebuilding them.")
@click.option("--health-timeout", default=120, type=int,
              help="Seconds to wait for healthy revision (default: 120).")
@click.pass_context
def deploy_app_cmd(
    ctx: click.Context,
    env: str,
    image_tag: str | None,
    run_id: str | None,
    receipt_path: str | None,
    dry_run: bool,
    registry_path: str | None,
    metadata_path: str | None,
    rollback_on_unhealthy: bool,
    cloud_build: bool,
    build_context: str,
    skip_build: bool,
    health_timeout: int,
    _runner: Callable | None = None,
) -> None:
    """Build + push + Bicep-deploy the reference app to Container Apps."""
    tag = image_tag or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    reg_path = Path(registry_path) if registry_path else _DEFAULT_REGISTRY_PATH
    runner = _runner or _default_runner
    run_token = ""
    if run_id:
        try:
            run_token = uuid.UUID(run_id).hex[:8]
        except ValueError as exc:
            raise click.ClickException("--run-id must be a valid UUID.") from exc
    base_name = f"fabric-kg-{run_token}" if run_token else "fabric-kg"

    try:
        metadata = load_agent_metadata(
            Path(metadata_path) if metadata_path else _DEFAULT_METADATA_PATH
        )
        env_cfg = metadata.env_config(env)
        acr_server = os.environ.get("ACR_LOGIN_SERVER", "") or env_cfg.acr.get("loginServer", "")
        acr_repo = env_cfg.acr.get("repository", "fabric-kg")
        resource_group = os.environ.get("AZURE_RESOURCE_GROUP", "") or env_cfg.resourceGroup
        subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "") or env_cfg.subscriptionId
    except AgentMetadataError as exc:
        click.echo(f"Error loading agent metadata: {exc}", err=True)
        sys.exit(1)

    if not acr_server:
        click.echo("Error: ACR loginServer not configured in agent-metadata.yaml.", err=True)
        sys.exit(1)

    tenant_id = os.environ.get("FABRIC_KG_TENANT_ID") or os.environ.get(
        "AZURE_TENANT_ID", ""
    )
    api_audience = os.environ.get("FABRIC_KG_AUDIENCE", "")
    api_scope = os.environ.get("FABRIC_KG_API_SCOPE", "")
    if not api_scope and api_audience:
        api_scope = f"{api_audience.rstrip('/')}/.default"
    required_api_role = os.environ.get("FABRIC_KG_REQUIRED_APP_ROLE", "")
    additional_allowed_callers = os.environ.get(
        "FABRIC_KG_ALLOWED_CALLER_OBJECT_IDS", ""
    )
    infra_outputs = _load_infra_outputs(env)
    serving_env = _load_serving_environment(env)
    search_config = serving_env.get("ai_search", {})
    blob_config = serving_env.get("blob_storage", {})
    azure_config = serving_env.get("azure", {})
    search_endpoint = _env_or_output(
        "FABRIC_KG_SEARCH_ENDPOINT", infra_outputs, "searchEndpoint"
    )
    search_index = _env_or_output(
        "FABRIC_KG_KB_INDEX",
        infra_outputs,
        "searchIndexName",
        f"kg-{env}-kg-chunks",
    )
    visual_index = _env_or_output(
        "FABRIC_KG_VISUAL_INDEX",
        infra_outputs,
        "visualIndexName",
        (
            search_config.get("index_prefix", "")
            + search_config.get("index_visual_assets", "kg-visual-assets")
        ),
    )
    blob_account_url = (
        os.environ.get("FABRIC_KG_BLOB_ACCOUNT_URL", "")
        or str(blob_config.get("endpoint", "")).rstrip("/")
    )
    blob_container = os.environ.get(
        "FABRIC_KG_BLOB_CONTAINER",
        str(blob_config.get("container", "")),
    )
    storage_resource_id = (
        os.environ.get("FABRIC_KG_STORAGE_ACCOUNT_RESOURCE_ID", "")
        or str(search_config.get("integrated_vectorization", {}).get("storage_resource_id", ""))
    )
    search_resource_id = os.environ.get(
        "FABRIC_KG_SEARCH_SERVICE_RESOURCE_ID",
        (
            f"/subscriptions/{subscription_id}/resourceGroups/"
            f"{azure_config.get('resource_group', resource_group)}/providers/"
            f"Microsoft.Search/searchServices/{search_config.get('service_name', '')}"
            if subscription_id and search_config.get("service_name")
            else ""
        ),
    )
    fabric_workspace_id = _env_or_output(
        "FABRIC_KG_FABRIC_WORKSPACE_ID", infra_outputs, "fabricWorkspaceId"
    )
    graph_model_id = _env_or_output(
        "FABRIC_KG_GRAPH_MODEL_ID", infra_outputs, "fabricGraphModelId"
    )
    identity_resource_id = _env_or_output(
        "FABRIC_KG_MANAGED_IDENTITY_RESOURCE_ID", infra_outputs, "identityId"
    )
    identity_client_id = _env_or_output(
        "FABRIC_KG_MANAGED_IDENTITY_CLIENT_ID", infra_outputs, "identityClientId"
    )
    identity_principal_id = _env_or_output(
        "FABRIC_KG_MANAGED_IDENTITY_PRINCIPAL_ID",
        infra_outputs,
        "identityPrincipalId",
    )
    create_identity = _is_true_env("FABRIC_KG_APP_CREATE_MANAGED_IDENTITY")
    identity_name = os.environ.get(
        "FABRIC_KG_APP_MANAGED_IDENTITY_NAME", f"fabric-kg-{env}-app-id"
    )
    graph_preview_ack = _is_true_env(
        "FABRIC_KG_GRAPH_PREVIEW_ACKNOWLEDGED"
    )
    downstream_access_confirmed = _is_true_env(
        "FABRIC_KG_DOWNSTREAM_ACCESS_CONFIRMED"
    )
    app_role_grant_confirmed = _is_true_env(
        "FABRIC_KG_API_APP_ROLE_GRANT_CONFIRMED"
    )
    api_image = f"{acr_server}/{acr_repo}/api:{tag}"
    ui_image = f"{acr_server}/{acr_repo}/ui:{tag}"
    bicep_file = str(Path("apps") / "infra" / "main.bicep")

    acr_name = acr_server.split(".")[0]  # e.g. "acrfabrickgdev" from "acrfabrickgdev.azurecr.io"

    click.echo(
        f"deploy-app: env={env}, tag={tag}"
        + (" [DRY RUN — planned only, no remote changes]" if dry_run else "")
    )
    click.echo(f"  API image  : {api_image}")
    click.echo(f"  UI  image  : {ui_image}")
    click.echo(f"  ACR        : {acr_server}")
    click.echo(f"  Bicep      : {bicep_file}")
    click.echo(f"  RG         : {resource_group}")
    click.echo(f"  Search     : {search_endpoint or '[missing]'} / {search_index}")
    click.echo(f"  Visual     : {visual_index or '[missing]'} / {blob_account_url or '[missing]'}")
    click.echo(
        f"  Fabric     : workspace={fabric_workspace_id or '[missing]'}, "
        f"graph={graph_model_id or '[missing]'}"
    )
    click.echo(
        "  Identity   : "
        + (f"create {identity_name}" if create_identity else (identity_resource_id or "[missing]"))
    )

    if dry_run:
        click.echo(
            "  Access gates: graph-preview="
            f"{graph_preview_ack}, downstream-access={downstream_access_confirmed}, "
            f"api-app-role={app_role_grant_confirmed}"
        )
        click.echo("\ndeploy-app: dry-run plan complete. No images built, no deployment performed.")
        return

    required_runtime = {
        "FABRIC_KG_TENANT_ID or AZURE_TENANT_ID": tenant_id,
        "FABRIC_KG_AUDIENCE": api_audience,
        "FABRIC_KG_API_SCOPE": api_scope,
        "FABRIC_KG_SEARCH_ENDPOINT or infra output searchEndpoint": search_endpoint,
        "FABRIC_KG_KB_INDEX": search_index,
        "FABRIC_KG_VISUAL_INDEX": visual_index,
        "FABRIC_KG_BLOB_ACCOUNT_URL": blob_account_url,
        "FABRIC_KG_BLOB_CONTAINER": blob_container,
        "FABRIC_KG_STORAGE_ACCOUNT_RESOURCE_ID": storage_resource_id,
        "FABRIC_KG_SEARCH_SERVICE_RESOURCE_ID": search_resource_id,
        "FABRIC_KG_FABRIC_WORKSPACE_ID or infra output fabricWorkspaceId": fabric_workspace_id,
        "FABRIC_KG_GRAPH_MODEL_ID or infra output fabricGraphModelId": graph_model_id,
    }
    if not create_identity:
        required_runtime.update(
            {
                "FABRIC_KG_MANAGED_IDENTITY_RESOURCE_ID or infra output identityId": identity_resource_id,
                "FABRIC_KG_MANAGED_IDENTITY_CLIENT_ID or infra output identityClientId": identity_client_id,
                "FABRIC_KG_MANAGED_IDENTITY_PRINCIPAL_ID or infra output identityPrincipalId": identity_principal_id,
            }
        )
    missing = [name for name, value in required_runtime.items() if not value]
    if missing:
        click.echo(
            "Error: deploy-app is missing required runtime configuration: "
            + ", ".join(missing),
            err=True,
        )
        sys.exit(1)
    missing_gates = [
        name
        for name, confirmed in (
            ("FABRIC_KG_GRAPH_PREVIEW_ACKNOWLEDGED=true", graph_preview_ack),
            ("FABRIC_KG_DOWNSTREAM_ACCESS_CONFIRMED=true", downstream_access_confirmed),
        )
        if not confirmed
    ]
    if required_api_role and not app_role_grant_confirmed:
        missing_gates.append("FABRIC_KG_API_APP_ROLE_GRANT_CONFIRMED=true")
    if missing_gates:
        click.echo(
            "Error: deploy-app requires explicit access/preview gates: "
            + ", ".join(missing_gates)
            + ". Grant the identity Search Index Data Reader, Fabric Workspace "
            "Viewer, and the API application role before retrying.",
            err=True,
        )
        sys.exit(1)

    # ── Step 1: Build immutable images ────────────────────────────────────────
    if cloud_build:
        build_cmds = [
            (
                api_image,
                [
                    "az", "acr", "build",
                    "--registry", acr_name,
                    "--image", f"{acr_repo}/api:{tag}",
                    "--file", "apps/api/Dockerfile",
                    build_context,
                ],
            ),
            (
                ui_image,
                [
                    "az", "acr", "build",
                    "--registry", acr_name,
                    "--image", f"{acr_repo}/ui:{tag}",
                    "--file", "apps/chainlit/Dockerfile",
                    build_context,
                ],
            ),
        ]
    else:
        build_cmds = [
            (api_image, ["docker", "build", "--platform", "linux/amd64",
                         "-t", api_image, "-f", "apps/api/Dockerfile", build_context]),
            (ui_image,  ["docker", "build", "--platform", "linux/amd64",
                         "-t", ui_image, "-f", "apps/chainlit/Dockerfile", build_context]),
        ]
    if skip_build:
        click.echo("\n  [OK] using existing API and UI images.")
    else:
        for img, cmd in build_cmds:
            click.echo(f"\n  $ {' '.join(cmd)}")
            result = runner(cmd)
            if result.returncode != 0:
                click.echo(f"  [FAIL] image build failed for {img}:\n{result.stderr}", err=True)
                sys.exit(result.returncode)
            click.echo(f"  [OK] built {img}")

    # ── Step 2: Push local builds (ACR cloud builds are already published) ───
    if not cloud_build and not skip_build:
        login_cmd = ["az", "acr", "login", "--name", acr_name]
        click.echo(f"\n  $ {' '.join(login_cmd)}")
        login_result = runner(login_cmd)
        if login_result.returncode != 0:
            click.echo(f"  [FAIL] ACR login failed:\n{login_result.stderr}", err=True)
            sys.exit(login_result.returncode)

        for img, _ in build_cmds:
            push_cmd = ["docker", "push", img]
            click.echo(f"  $ {' '.join(push_cmd)}")
            result = runner(push_cmd)
            if result.returncode != 0:
                click.echo(f"  [FAIL] push failed for {img}:\n{result.stderr}", err=True)
                sys.exit(result.returncode)
            click.echo(f"  [OK] pushed {img}")

    if not additional_allowed_callers:
        additional_allowed_callers = _current_azure_principal_id(runner)

    api_app_name = f"{base_name}-{env}-api"
    ui_app_name = f"{base_name}-{env}-ui"
    previous_api_revision = _active_revision_name(
        runner, api_app_name, resource_group
    )
    previous_ui_revision = _active_revision_name(
        runner, ui_app_name, resource_group
    )

    # ── Step 3: Bicep deployment ──────────────────────────────────────────────
    bicep_cmd = [
        "az", "deployment", "group", "create",
        "--resource-group", resource_group,
        "--template-file", bicep_file,
        "--parameters",
        f"imageTag={tag}",
        f"acrLoginServer={acr_server}",
        f"acrRepository={acr_repo}",
        f"environment={env}",
        f"baseName={base_name}",
        f"runId={run_id or ''}",
        f"tenantId={tenant_id}",
        f"apiAudience={api_audience}",
        f"apiScope={api_scope}",
        f"requiredApiAppRole={required_api_role}",
        f"additionalAllowedCallerObjectIds={additional_allowed_callers}",
        f"searchEndpoint={search_endpoint}",
        f"searchIndexName={search_index}",
        f"visualIndexName={visual_index}",
        f"blobAccountUrl={blob_account_url}",
        f"blobContainer={blob_container}",
        f"storageAccountResourceId={storage_resource_id}",
        f"searchServiceResourceId={search_resource_id}",
        f"fabricWorkspaceId={fabric_workspace_id}",
        f"graphModelId={graph_model_id}",
        "graphPreviewAcknowledged=true",
        f"createManagedIdentity={str(create_identity).lower()}",
        f"managedIdentityName={identity_name}",
        f"managedIdentityResourceId={identity_resource_id}",
        f"managedIdentityClientId={identity_client_id}",
        f"managedIdentityPrincipalId={identity_principal_id}",
        "downstreamAccessConfirmed=true",
        f"apiAppRoleGrantConfirmed={str(app_role_grant_confirmed).lower()}",
        "--output", "json",
    ]
    if subscription_id and subscription_id != "00000000-0000-0000-0000-000000000000":
        bicep_cmd.extend(["--subscription", subscription_id])

    click.echo(f"\n  $ {' '.join(bicep_cmd)}")
    bicep_result = runner(bicep_cmd)
    if bicep_result.returncode != 0:
        click.echo(f"  [FAIL] Bicep deployment failed:\n{bicep_result.stderr}", err=True)
        sys.exit(bicep_result.returncode)
    click.echo("  [OK] Bicep deployment complete.")

    # ── Step 4: Poll for healthy revision ────────────────────────────────────
    deployment_outputs = _deployment_outputs(bicep_result.stdout)
    output_revisions = {
        key: str(value)
        for key, value in deployment_outputs.items()
        if key in {"apiRevisionName", "uiRevisionName"}
    }
    api_revision_name = output_revisions.get(
        "apiRevisionName", f"{api_app_name}--{tag}"
    )
    ui_revision_name = output_revisions.get(
        "uiRevisionName", f"{ui_app_name}--{tag}"
    )
    api_healthy = _poll_revision_health(
        runner, api_app_name, resource_group, tag, timeout_s=health_timeout
    )
    ui_healthy = _poll_revision_health(
        runner, ui_app_name, resource_group, tag, timeout_s=health_timeout
    )

    if not (api_healthy and ui_healthy):
        unhealthy = [
            name
            for name, healthy in (
                (api_revision_name, api_healthy),
                (ui_revision_name, ui_healthy),
            )
            if not healthy
        ]
        click.echo(
            f"  [WARN] revisions not healthy after {health_timeout}s: "
            + ", ".join(unhealthy),
            err=True,
        )
        click.echo(
            "  Verify Search Index Data Reader, Fabric Workspace Viewer, "
            "Graph preview acknowledgement, and the UI identity's API app-role "
            "grant. The UI readiness probe validates its managed-identity call "
            "to the authenticated API readiness endpoint.",
            err=True,
        )
        if rollback_on_unhealthy:
            _rollback_revision(
                runner,
                api_app_name,
                resource_group,
                failed_revision=api_revision_name,
                previous_revision=previous_api_revision,
            )
            _rollback_revision(
                runner,
                ui_app_name,
                resource_group,
                failed_revision=ui_revision_name,
                previous_revision=previous_ui_revision,
            )
        sys.exit(1)

    click.echo(f"  [OK] API and UI revisions for {tag} are healthy.")

    # ── Step 5: Record canonical lineage (only here — after confirmed health) ─
    _record_app_lineage(
        reg_path, env, tag,
        [
            (
                "api",
                api_image,
                str(deployment_outputs.get("apiContainerAppId") or "") or None,
            ),
            (
                "ui",
                ui_image,
                str(deployment_outputs.get("uiContainerAppId") or "") or None,
            ),
        ],
        run_id=run_id,
    )
    if receipt_path:
        receipt = {
            "environment": env,
            "run_id": run_id,
            "image_tag": tag,
            "api_image": api_image,
            "ui_image": ui_image,
            "outputs": deployment_outputs,
        }
        target = Path(receipt_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    click.echo("  lineage recorded.")
    click.echo("\ndeploy-app: done.")


def _poll_revision_health(
    runner: Callable,
    app_name: str,
    resource_group: str,
    revision_suffix: str,
    *,
    timeout_s: int = 120,
    poll_interval_s: int = 10,
) -> bool:
    """Poll the Container Apps revision until healthy or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cmd = [
            "az", "containerapp", "revision", "list",
            "--name", app_name,
            "--resource-group", resource_group,
            "--output", "json",
        ]
        result = runner(cmd)
        if result.returncode == 0:
            try:
                revisions = json.loads(result.stdout or "[]")
                for rev in revisions:
                    name = rev.get("name", "")
                    properties = rev.get("properties", {})
                    health = properties.get("healthState", "")
                    active = properties.get("active", True)
                    if (
                        revision_suffix in name
                        and health == "Healthy"
                        and active is not False
                    ):
                        return True
            except (json.JSONDecodeError, TypeError):
                pass
        time.sleep(poll_interval_s)
    return False


def _active_revision_name(
    runner: Callable,
    app_name: str,
    resource_group: str,
) -> str | None:
    cmd = [
        "az", "containerapp", "revision", "list",
        "--name", app_name,
        "--resource-group", resource_group,
        "--output", "json",
    ]
    result = runner(cmd)
    if result.returncode != 0:
        return None
    try:
        revisions = json.loads(result.stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    candidates = [
        rev
        for rev in revisions
        if rev.get("properties", {}).get("active", True)
        and rev.get("properties", {}).get("trafficWeight", 0) > 0
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda rev: rev.get("properties", {}).get("createdTime", ""),
        reverse=True,
    )
    return str(candidates[0].get("name") or "") or None


def _deployment_outputs(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    outputs = payload.get("properties", {}).get("outputs", {})
    return {
        key: str(value.get("value", ""))
        for key, value in outputs.items()
        if isinstance(value, dict)
        and value.get("value")
    }


def _deployment_revision_names(stdout: str) -> dict[str, str]:
    return {
        key: str(value)
        for key, value in _deployment_outputs(stdout).items()
        if key in {"apiRevisionName", "uiRevisionName"}
    }


def _rollback_revision(
    runner: Callable,
    app_name: str,
    resource_group: str,
    *,
    failed_revision: str,
    previous_revision: str | None,
) -> bool:
    """Restore traffic to a known prior revision, then deactivate the failed one."""
    click.echo(f"  Rolling back {app_name}...", err=True)
    if not previous_revision or previous_revision == failed_revision:
        click.echo(
            "  [WARN] No distinct previously active revision is available; "
            "automatic rollback is not possible.",
            err=True,
        )
        return False

    traffic_cmd = [
        "az", "containerapp", "ingress", "traffic", "set",
        "--name", app_name,
        "--resource-group", resource_group,
        "--revision-weight", f"{previous_revision}=100",
    ]
    traffic_result = runner(traffic_cmd)
    if traffic_result.returncode != 0:
        click.echo(
            f"  [WARN] Failed to restore traffic:\n{traffic_result.stderr}",
            err=True,
        )
        return False

    deactivate_cmd = [
        "az", "containerapp", "revision", "deactivate",
        "--name", app_name,
        "--resource-group", resource_group,
        "--revision", failed_revision,
    ]
    deactivate_result = runner(deactivate_cmd)
    if deactivate_result.returncode != 0:
        click.echo(
            f"  [WARN] Failed to deactivate unhealthy revision:\n"
            f"{deactivate_result.stderr}",
            err=True,
        )
        return False
    click.echo(f"  Rollback restored {previous_revision}.", err=True)
    return True


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


_TEST_EPILOG = """\b
Routes offline evaluation cases through actual routing/answer logic (not stubs).
Live cases (--integration) invoke the deployed Foundry agent/API endpoint.
Results are persisted under .foundry/results/<environment>/.

Example:
  fabric-kg app test
  fabric-kg app test --smoke             # smoke markers only
  fabric-kg app test --integration       # requires live env vars
  fabric-kg app test --dataset .foundry/datasets/eval_dataset_v1.jsonl
"""


@app_cmd.command("test", epilog=_TEST_EPILOG)
@click.option("--integration", "run_integration", is_flag=True, default=False,
              help="Include integration tests (requires live env vars).")
@click.option("--smoke", "smoke_only", is_flag=True, default=False,
              help="Run smoke tests only.")
@click.option("--env", default="offline",
              help="Environment label for result persistence.")
@click.option("--dataset", default=None, type=click.Path(),
              help="Override path to eval dataset JSONL.")
@click.pass_context
def test_cmd(
    ctx: click.Context,
    run_integration: bool,
    smoke_only: bool,
    env: str,
    dataset: str | None,
    _runner: Callable | None = None,
) -> None:
    """Run agent/app test suite and evaluation with actual routing."""
    runner = _runner or _default_runner

    # ── pytest ────────────────────────────────────────────────────────────────
    args = [
        sys.executable, "-m", "pytest",
        "tests/test_reference_app_agent.py",
        "tests/test_reference_app_api.py",
        "tests/test_reference_app_cli.py",
        "--override-ini=addopts=",
        "--tb=short", "-q",
    ]
    if smoke_only:
        args += ["-m", "smoke"]
    elif run_integration:
        args += ["-m", "smoke or integration"]
    else:
        args += ["-m", "not integration"]

    click.echo(f"Running: {' '.join(args)}")
    test_result = runner(args)
    if test_result.stdout:
        click.echo(test_result.stdout)
    if test_result.stderr:
        click.echo(test_result.stderr, err=True)

    # ── Offline evaluation through ACTUAL routing logic ───────────────────────
    dataset_path = dataset or None
    cases = load_eval_dataset(dataset_path)
    if not cases:
        click.echo("No evaluation cases found; skipping offline eval.")
        sys.exit(test_result.returncode)

    click.echo(f"\nOffline evaluation: {len(cases)} cases via actual routing.")
    actual_responses = _run_offline_evaluation(cases)

    summary = run_evaluation(
        actual_responses,
        dataset_path=dataset_path,
        environment=env,
        persist_results=True,
    )

    click.echo(f"  Pass rate : {summary.pass_rate:.0%} ({summary.passed}/{summary.total})")
    click.echo(f"  All gates : {'PASS' if summary.all_gates_passed else 'FAIL'}")
    for g in summary.gate_results:
        status = "✓" if g.passed else "✗"
        click.echo(f"    {status} {g.message}")
    for v in summary.threshold_violations:
        click.echo(f"  [VIOLATION] {v}", err=True)

    results_dir = _RESULTS_ROOT / env
    click.echo(f"  Results persisted → {results_dir}")

    if summary.threshold_violations:
        click.echo("Evaluation gates FAILED.", err=True)
        sys.exit(max(1, test_result.returncode))

    sys.exit(test_result.returncode)


def _run_offline_evaluation(cases: list[EvalCase]) -> list[dict]:
    """Route each eval case through actual answer logic (not stubs).

    This uses the API's `_answer_question` function directly so that responses
    are genuine, not manufactured from expected values.
    """
    from fabric_kg_builder.app.api import _answer_question
    from fabric_kg_builder.agent.tools.kb_tool import KnowledgeBaseTool
    from fabric_kg_builder.agent.tools.fabric_data import FabricDataAgentAdapter

    kb = KnowledgeBaseTool(index_name="offline-eval", _client=None)
    graph = FabricDataAgentAdapter(_client=None)

    responses: list[dict] = []
    for case in cases:
        t0 = time.monotonic()
        try:
            answer, route_type, citations, refused = _answer_question(
                question=case.input,
                kb=kb,
                graph=graph,
            )
        except Exception as exc:
            answer = f"[error: {exc}]"
            route_type = ""
            citations = []
            refused = False
        latency_ms = (time.monotonic() - t0) * 1000

        responses.append({
            "case_id": case.id,
            "route_type": route_type,
            "answer": answer,
            "citations": [c.to_safe_dict() for c in citations],
            "refused": refused,
            "latency_ms": latency_ms,
        })
    return responses


@app_cmd.command("publish-structured")
@click.option(
    "--l4-run",
    "l4_run",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Sealed L4 run directory holding the projection tables and receipt.",
)
@click.option(
    "--l3-root",
    "l3_root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="L3 state root searched for the manifest the L4 receipt names.",
)
@click.option(
    "--workspace-id",
    required=True,
    help="Fabric workspace GUID that hosts the four L5a targets.",
)
@click.option(
    "--name-prefix",
    default="fabric-kg-024",
    show_default=True,
    help="Release-owned item name prefix; existing items are never adopted.",
)
@click.option(
    "--dry-run/--live",
    default=True,
    show_default=True,
    help="Compile a plan by default; live requires exact plan-hash approval.",
)
@click.option(
    "--approve-live",
    default=None,
    help="Exact plan hash authorising live publication.",
)
@click.option(
    "--plan",
    "plan_path",
    default="build/release/l5a-0.2.4-plan.json",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
@click.option(
    "--materialize",
    "materialize_dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Write the compiled tables and target definitions to this directory.",
)
def publish_structured_cmd(
    l4_run: Path,
    l3_root: Path,
    workspace_id: str,
    name_prefix: str,
    dry_run: bool,
    approve_live: str | None,
    plan_path: Path,
    materialize_dir: Path | None,
) -> None:
    """Compile and plan the L5a structured publication of a sealed L4 run.

    Live publication of the four Fabric targets is a capability NO-GO on the
    0.2.4 line: Fabric's item control plane returns an empty ETag and ignores
    ``If-Match`` on delete, so creating or rolling back a release-owned item
    cannot be fenced by compare-and-swap. The plan records that verdict
    explicitly rather than attempting an unfenced mutation.
    """

    from fabric_kg_builder.contracts.base import (
        canonical_json,
        canonical_sha256,
    )
    from fabric_kg_builder.version import RELEASE_VERSION as _rv
    from fabric_kg_builder.deploy.fabric_l5a_targets import (
        FabricL5aTargetClient,
    )
    from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
    from fabric_kg_builder.serving.l5a_crosswalk import (
        compile_access_policy,
        compile_governed_assets,
        compile_publication_crosswalk,
    )
    from fabric_kg_builder.serving.structured_publication import (
        compile_l5a_publication,
    )

    source = SealedL4ServingSource.from_run(
        l4_run,
        input_manifest_search_roots=(l3_root,),
    )
    target_ids = {
        "parquet": f"target:{name_prefix}-lakehouse",
        "semantic_model": f"target:{name_prefix}-semantic-model",
        "ontology": f"target:{name_prefix}-ontology",
        "graph": f"target:{name_prefix}-graph",
    }
    crosswalk = compile_publication_crosswalk(source)
    policy = compile_access_policy(
        source,
        access_policy_id=f"access-policy:{name_prefix}",
        principal_id=f"principal:{name_prefix}-publisher",
        resource_scope_id=f"resource:fabric-workspace:{workspace_id}",
        authorization_resource_id=f"authorization-resource:{name_prefix}",
    )
    assets = compile_governed_assets(
        source,
        crosswalks=(crosswalk,),
        access_policy=policy,
        target_ids=target_ids,
        workspace_id=workspace_id,
    )
    compiled = compile_l5a_publication(
        source,
        crosswalks=(crosswalk,),
        access_policy=policy,
        governed_assets=assets,
        target_ids=target_ids,
    )
    capabilities = FabricL5aTargetClient(
        workspace_id=workspace_id,
        token="",
    ).capability_report()
    blocked = sorted(
        name
        for name, value in capabilities.items()
        if name.endswith(".create") and value is False
    )
    plan: dict[str, Any] = {
        "plan_version": "1.0.0",
        "release_version": _rv,
        "workspace_id": workspace_id,
        "source_projection_id": compiled.definitions["parquet"][
            "source_projection_id"
        ],
        "source_projection_hash": compiled.definitions["parquet"][
            "source_projection_hash"
        ],
        "crosswalk_hash": crosswalk.crosswalk_hash,
        "stable_id_lock_hash": crosswalk.stable_id_lock_hash,
        "access_policy_hash": policy.policy_hash,
        "target_ids": dict(sorted(target_ids.items())),
        "definition_hashes": {
            kind: canonical_sha256(compiled.definitions[kind])
            for kind in sorted(compiled.definitions)
        },
        "tables": [
            {
                "table_id": snapshot.table_id,
                "row_count": snapshot.row_count,
                "schema_hash": snapshot.schema_hash,
                "row_fingerprint": snapshot.row_fingerprint,
            }
            for snapshot in compiled.table_snapshots
        ],
        "capabilities": capabilities,
        "blocked_capabilities": blocked,
        "live_publication_supported": not blocked,
    }
    if materialize_dir is not None:
        import pyarrow.parquet as pq

        for table_id, table in sorted(compiled.tables.items()):
            path = materialize_dir / "tables" / f"{table_id}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, path, compression="snappy", version="2.6")
            if pq.read_table(path).num_rows != table.num_rows:
                raise click.ClickException(
                    f"materialized Parquet row count drifted for {table_id}"
                )
        for kind in sorted(compiled.definitions):
            path = materialize_dir / "definitions" / f"{kind}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                canonical_json(compiled.definitions[kind]) + "\n",
                encoding="utf-8",
            )
        click.echo(f"materialized={materialize_dir}")

    plan["plan_hash"] = canonical_sha256(plan)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(canonical_json(plan) + "\n", encoding="utf-8")
    click.echo(f"plan_hash={plan['plan_hash']}")
    click.echo(f"plan={plan_path}")
    click.echo(f"tables={len(plan['tables'])}")
    if dry_run:
        click.echo("mode=dry-run")
        return
    if approve_live != plan["plan_hash"]:
        raise click.ClickException(
            "--live requires --approve-live with the exact plan hash"
        )
    raise click.ClickException(
        "live L5a publication is a capability NO-GO on this release line: "
        + str(capabilities["fabric.capability_reason"])
    )


# ---------------------------------------------------------------------------
# Standalone entry-point (also usable without main.py registration)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app_cmd()
